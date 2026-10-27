package app

import (
	"bufio"
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"net/url"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/neelsatyavolu/ugreen-drive/cli/internal/api"
	"github.com/neelsatyavolu/ugreen-drive/cli/internal/config"
	"github.com/neelsatyavolu/ugreen-drive/cli/internal/davfs"
)

const davPassword = "correct-horse-battery-staple"

type davHarness struct {
	fs      *davfs.FS
	drive   *fakeDrive
	srv     *httptest.Server
	mu      sync.Mutex
	uploads []davfs.Upload
}

func (h *davHarness) uploadEvents() []davfs.Upload {
	h.mu.Lock()
	defer h.mu.Unlock()
	return append([]davfs.Upload(nil), h.uploads...)
}

func newDavHarness(t *testing.T) *davHarness {
	t.Helper()
	drive, driveSrv := newFakeDrive(t)
	base, _ := url.Parse(driveSrv.URL)
	client := &api.Client{Base: base, Token: testToken, HTTP: driveSrv.Client()}
	fs := davfs.New(client, t.TempDir())
	h := &davHarness{drive: drive, fs: fs}
	fs.OnUpload = func(u davfs.Upload) {
		h.mu.Lock()
		h.uploads = append(h.uploads, u)
		h.mu.Unlock()
	}
	h.srv = httptest.NewServer(davfs.Handler(fs, "/Ugreen Drive", davPassword, t.Logf))
	t.Cleanup(h.srv.Close)
	return h
}

// url builds a WebDAV URL from unescaped path segments below the volume.
func (h *davHarness) url(segments ...string) string {
	parts := []string{h.srv.URL, "Ugreen%20Drive"}
	for _, s := range segments {
		parts = append(parts, url.PathEscape(s))
	}
	return strings.Join(parts, "/")
}

func (h *davHarness) do(t *testing.T, method, target string, body string, headers ...string) (int, string) {
	t.Helper()
	req, err := http.NewRequest(method, target, strings.NewReader(body))
	if err != nil {
		t.Fatal(err)
	}
	req.SetBasicAuth(davfs.User, davPassword)
	for i := 0; i+1 < len(headers); i += 2 {
		if headers[i] == "Host" {
			req.Host = headers[i+1]
		} else {
			req.Header.Set(headers[i], headers[i+1])
		}
	}
	res, err := h.srv.Client().Do(req)
	if err != nil {
		t.Fatal(err)
	}
	defer res.Body.Close()
	raw, _ := io.ReadAll(res.Body)
	return res.StatusCode, string(raw)
}

func (h *davHarness) propfind(t *testing.T, target string) (int, string) {
	return h.do(t, "PROPFIND", target, "", "Depth", "1")
}

func (h *davHarness) driveHas(p string) (string, bool) {
	h.drive.mu.Lock()
	defer h.drive.mu.Unlock()
	f, ok := h.drive.files[p]
	if !ok {
		return "", false
	}
	return string(f.data), true
}

func TestWebdavRequiresPasswordAndLoopbackHost(t *testing.T) {
	h := newDavHarness(t)
	res, err := http.Get(h.url())
	if err != nil {
		t.Fatal(err)
	}
	res.Body.Close()
	if res.StatusCode != http.StatusUnauthorized || res.Header.Get("WWW-Authenticate") == "" {
		t.Fatalf("no password: got %d", res.StatusCode)
	}
	req, _ := http.NewRequest("PROPFIND", h.url(), nil)
	req.SetBasicAuth(davfs.User, "wrong-password-wrong-password")
	res, _ = http.DefaultClient.Do(req)
	res.Body.Close()
	if res.StatusCode != http.StatusUnauthorized {
		t.Fatalf("wrong password: got %d", res.StatusCode)
	}
	if code, _ := h.do(t, "PROPFIND", h.url(), "", "Host", "evil.example.com", "Depth", "1"); code != http.StatusForbidden {
		t.Fatalf("rebinding host: got %d", code)
	}
}

func TestWebdavRootListsShares(t *testing.T) {
	h := newDavHarness(t)
	code, body := h.propfind(t, h.url()+"/")
	if code != http.StatusMultiStatus {
		t.Fatalf("PROPFIND root: %d %s", code, body)
	}
	for _, want := range []string{"Ugreen%20Drive/Ugreen%20Drive/", "Ugreen%20Drive/Photos/", "Ugreen%20Drive/My%20folder/"} {
		if !strings.Contains(body, want) {
			t.Errorf("root listing missing %s:\n%s", want, body)
		}
	}
}

func TestWebdavListAndReadWithRange(t *testing.T) {
	h := newDavHarness(t)
	h.drive.dir("Documents")
	h.drive.put("Documents/cut.txt", "0123456789")
	h.drive.put("Documents/.DS_Store", "server junk")

	code, body := h.propfind(t, h.url("My folder", "Documents")+"/")
	if code != http.StatusMultiStatus || !strings.Contains(body, "cut.txt") || strings.Contains(body, "DS_Store") {
		t.Fatalf("PROPFIND: %d %s", code, body)
	}
	if h.drive.lastShare != "~alex" {
		t.Fatalf("share header = %q, want ~alex", h.drive.lastShare)
	}
	if code, body := h.do(t, "GET", h.url("My folder", "Documents", "cut.txt"), ""); code != 200 || body != "0123456789" {
		t.Fatalf("GET: %d %q", code, body)
	}
	code, body = h.do(t, "GET", h.url("My folder", "Documents", "cut.txt"), "", "Range", "bytes=4-6")
	if code != http.StatusPartialContent || body != "456" {
		t.Fatalf("ranged GET: %d %q", code, body)
	}
	if h.drive.rangeReads == 0 {
		t.Fatal("ranged GET downloaded the whole file instead of asking the Drive for a range")
	}
}

func TestWebdavPutUploadsSmallAndChunked(t *testing.T) {
	h := newDavHarness(t)
	if code, body := h.do(t, "PUT", h.url("Ugreen Drive", "small.txt"), "hello"); code != http.StatusCreated {
		t.Fatalf("PUT small: %d %s", code, body)
	}
	if got, _ := h.driveHas("small.txt"); got != "hello" {
		t.Fatalf("small.txt = %q", got)
	}
	big := strings.Repeat("x", api.ChunkThreshold+123)
	if code, body := h.do(t, "PUT", h.url("Ugreen Drive", "big.bin"), big); code != http.StatusCreated {
		t.Fatalf("PUT big: %d %s", code, body)
	}
	if got, _ := h.driveHas("big.bin"); got != big {
		t.Fatalf("big.bin has %d bytes, want %d", len(got), len(big))
	}
	if h.drive.chunkPuts == 0 {
		t.Fatal("big file did not use chunked upload")
	}
	// Overwrite in place.
	if code, _ := h.do(t, "PUT", h.url("Ugreen Drive", "small.txt"), "bye"); code/100 != 2 {
		t.Fatalf("overwrite: %d", code)
	}
	if got, _ := h.driveHas("small.txt"); got != "bye" {
		t.Fatalf("after overwrite small.txt = %q", got)
	}
	if code, _ := h.do(t, "PUT", h.url("Ugreen Drive", "missing", "a.txt"), "x"); code/100 == 2 {
		t.Fatalf("PUT into missing folder succeeded: %d", code)
	}
}

func TestWebdavHiddenFilesStayLocal(t *testing.T) {
	h := newDavHarness(t)
	for _, name := range []string{"._clip.mov", ".DS_Store"} {
		if code, body := h.do(t, "PUT", h.url("Ugreen Drive", name), "meta"); code != http.StatusCreated {
			t.Fatalf("PUT %s: %d %s", name, code, body)
		}
		if _, ok := h.driveHas(name); ok {
			t.Fatalf("%s was uploaded to the Drive", name)
		}
		if code, body := h.do(t, "GET", h.url("Ugreen Drive", name), ""); code != 200 || body != "meta" {
			t.Fatalf("GET %s: %d %q", name, code, body)
		}
	}
	if _, body := h.propfind(t, h.url("Ugreen Drive")+"/"); !strings.Contains(body, "._clip.mov") {
		t.Fatalf("listing misses local file:\n%s", body)
	}
	if code, _ := h.do(t, "DELETE", h.url("Ugreen Drive", "._clip.mov"), ""); code != http.StatusNoContent {
		t.Fatalf("DELETE: %d", code)
	}
	if code, _ := h.do(t, "GET", h.url("Ugreen Drive", "._clip.mov"), ""); code != http.StatusNotFound {
		t.Fatalf("GET after DELETE: %d", code)
	}
	// Finder may keep metadata even where it can't write real files.
	if code, _ := h.do(t, "PUT", h.url("Photos", ".DS_Store"), "meta"); code != http.StatusCreated {
		t.Fatalf("PUT .DS_Store in read-only share: %d", code)
	}
}

func TestWebdavSafeSaveThroughTempName(t *testing.T) {
	h := newDavHarness(t)
	h.drive.put("essay.docx", "draft 1")
	move := func(from, to string) {
		t.Helper()
		code, body := h.do(t, "MOVE", h.url("Ugreen Drive", from), "", "Destination", h.url("Ugreen Drive", to), "Overwrite", "T")
		if code/100 != 2 {
			t.Fatalf("MOVE %s → %s: %d %s", from, to, code, body)
		}
	}
	// Word-style save: write a temp file, move the original aside, move the
	// temp into place, delete the backup.
	if code, _ := h.do(t, "PUT", h.url("Ugreen Drive", "~wrd0001.tmp"), "draft 2"); code != http.StatusCreated {
		t.Fatalf("PUT temp: %d", code)
	}
	move("essay.docx", "~wrl0002.tmp")
	if code, _ := h.propfind(t, h.url("Ugreen Drive", "~wrl0002.tmp")); code != http.StatusMultiStatus {
		t.Fatalf("moved-aside original vanished: %d", code)
	}
	move("~wrd0001.tmp", "essay.docx")
	if code, _ := h.do(t, "DELETE", h.url("Ugreen Drive", "~wrl0002.tmp"), ""); code != http.StatusNoContent {
		t.Fatalf("DELETE backup: %d", code)
	}
	if got, _ := h.driveHas("essay.docx"); got != "draft 2" {
		t.Fatalf("essay.docx = %q, want draft 2", got)
	}
	h.drive.mu.Lock()
	defer h.drive.mu.Unlock()
	for p := range h.drive.files {
		if strings.HasSuffix(p, ".tmp") {
			t.Errorf("temp file left on the Drive: %s", p)
		}
	}
}

func TestWebdavFoldersMoveAndDelete(t *testing.T) {
	h := newDavHarness(t)
	if code, _ := h.do(t, "MKCOL", h.url("Ugreen Drive", "Documents"), ""); code != http.StatusCreated {
		t.Fatalf("MKCOL: %d", code)
	}
	h.do(t, "MKCOL", h.url("Ugreen Drive", "Archive"), "")
	h.do(t, "PUT", h.url("Ugreen Drive", "Documents", "ep1.txt"), "one")
	code, body := h.do(t, "MOVE", h.url("Ugreen Drive", "Documents", "ep1.txt"), "",
		"Destination", h.url("Ugreen Drive", "Archive", "episode 1.txt"))
	if code/100 != 2 {
		t.Fatalf("MOVE with rename: %d %s", code, body)
	}
	if got, _ := h.driveHas("Archive/episode 1.txt"); got != "one" {
		t.Fatalf("moved file = %q", got)
	}
	if code, _ := h.do(t, "DELETE", h.url("Ugreen Drive", "Documents"), ""); code != http.StatusNoContent {
		t.Fatalf("DELETE folder: %d", code)
	}
	if _, ok := h.driveHas("Documents"); ok {
		t.Fatal("folder still on the Drive")
	}
	code, _ = h.do(t, "MOVE", h.url("Ugreen Drive", "Archive"), "", "Destination", h.url("My folder", "Archive"))
	if code/100 == 2 {
		t.Fatalf("MOVE between shares succeeded: %d", code)
	}
}

func TestWebdavReadOnlyShare(t *testing.T) {
	h := newDavHarness(t)
	if code, _ := h.do(t, "PUT", h.url("Photos", "new.jpg"), "img"); code/100 == 2 {
		t.Fatalf("PUT in read-only share: %d", code)
	}
	if _, ok := h.driveHas("new.jpg"); ok {
		t.Fatal("file written to read-only share")
	}
	if code, _ := h.do(t, "PUT", h.url("new-share-level-file"), "x"); code/100 == 2 {
		t.Fatalf("PUT at the share list root: %d", code)
	}
}

// TestWebdavCommand runs `ugd webdav` the way the Mac app does.
func TestWebdavCommand(t *testing.T) {
	drive, driveSrv := newFakeDrive(t)
	drive.put("hello.txt", "hi")
	drive.put("later.txt", "not read yet") // small reads are cached: this one isn't
	dir := t.TempDir()
	if err := config.Save(dir, config.Config{Server: driveSrv.URL}); err != nil {
		t.Fatal(err)
	}
	stdinR, stdinW := io.Pipe()
	stdoutR, stdoutW := io.Pipe()
	var stderr bytes.Buffer
	env := Env{
		Stdin: stdinR, Stdout: stdoutW, Stderr: &stderr, ConfigDir: dir,
		Tokens: config.MemoryStore{strings.TrimPrefix(driveSrv.URL, "http://"): testToken},
		HTTP:   driveSrv.Client(), Getenv: func(string) string { return "" },
	}
	// No keep-alives: an idle connection would hold up the helper's Shutdown.
	client := &http.Client{Transport: &http.Transport{DisableKeepAlives: true}}
	exit := make(chan int, 1)
	go func() { exit <- Run(context.Background(), []string{"webdav"}, env); stdoutW.Close() }()
	go io.WriteString(stdinW, davPassword+"\n")

	// Read events continuously (like the app), so the helper never blocks on stdout.
	lines := make(chan map[string]any, 256)
	go func() {
		scanner := bufio.NewScanner(stdoutR)
		for scanner.Scan() {
			var raw map[string]any
			if json.Unmarshal(scanner.Bytes(), &raw) == nil {
				lines <- raw
			}
		}
		close(lines)
	}()
	nextRaw := func() map[string]any {
		t.Helper()
		select {
		case raw, ok := <-lines:
			if !ok {
				t.Fatalf("no event; stderr: %s", stderr.String())
			}
			return raw
		case <-time.After(10 * time.Second):
			t.Fatalf("no event within 10s; stderr: %s", stderr.String())
		}
		return nil
	}
	next := func() map[string]string {
		t.Helper()
		raw := nextRaw()
		ev := map[string]string{}
		for k, v := range raw {
			if s, ok := v.(string); ok {
				ev[k] = s
			}
		}
		return ev
	}
	ready := next()
	if ready["event"] != "ready" || !strings.HasPrefix(ready["url"], "http://127.0.0.1:") ||
		!strings.HasSuffix(ready["url"], "/Ugreen%20Drive/") {
		t.Fatalf("ready event = %v", ready)
	}
	get := func(name ...string) int {
		file := "hello.txt"
		if len(name) > 0 {
			file = name[0]
		}
		req, _ := http.NewRequest("GET", ready["url"]+"Ugreen%20Drive/"+file, nil)
		req.SetBasicAuth(davfs.User, davPassword)
		res, err := client.Do(req)
		if err != nil {
			t.Fatal(err)
		}
		res.Body.Close()
		return res.StatusCode
	}
	if code := get(); code != 200 {
		t.Fatalf("GET through command: %d", code)
	}
	put, _ := http.NewRequest("PUT", ready["url"]+"Ugreen%20Drive/new.txt", strings.NewReader("hello"))
	put.SetBasicAuth(davfs.User, davPassword)
	putDone := make(chan int, 1)
	go func() { // events are written while the PUT is in flight; keep reading them
		res, err := client.Do(put)
		if err != nil {
			putDone <- 0
			return
		}
		res.Body.Close()
		putDone <- res.StatusCode
	}()
	// The Mac app reads these lines; keep the shape stable.
	sawWriting := false
	for {
		ev := nextRaw()
		if ev["event"] == "writing" && ev["open"] == 1.0 {
			sawWriting = true
		}
		if ev["event"] == "upload" && ev["state"] == "done" {
			if ev["path"] != "Ugreen Drive/new.txt" || ev["size"] != 5.0 || ev["sent"] != 5.0 || ev["id"] == nil {
				t.Fatalf("upload event = %v", ev)
			}
			break
		}
	}
	if code := <-putDone; code != http.StatusCreated {
		t.Fatalf("PUT through command: %d", code)
	}
	if !sawWriting {
		t.Fatal(`no {"event":"writing","open":1} while the PUT was in flight`)
	}
	drive.mu.Lock()
	drive.revoked = true
	drive.mu.Unlock()
	time.Sleep(10 * time.Millisecond)
	get("later.txt")
	for {
		if ev := next(); ev["event"] == "signed_out" {
			break
		}
	}
	select {
	case code := <-exit:
		if code != ExitAuth {
			t.Fatalf("exit %d, want %d", code, ExitAuth)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("server did not stop after sign-out")
	}
}

func TestWebdavCommandRejectsBadInput(t *testing.T) {
	h := newHarness(t)
	h.env.Stdin = strings.NewReader("short\n")
	if code := h.run("webdav"); code != ExitUsage {
		t.Fatalf("short password: exit %d", code)
	}
	h.env.Stdin = strings.NewReader(davPassword + "\n")
	if code := h.run("webdav", "--addr", "0.0.0.0:0"); code != ExitUsage {
		t.Fatalf("non-loopback addr: exit %d", code)
	}
}

func (h *davHarness) set(fn func(d *fakeDrive)) {
	h.drive.mu.Lock()
	defer h.drive.mu.Unlock()
	fn(h.drive)
}

// Finder sends LOCK before writing; if the Drive can't be listed at that
// moment the file must not be replaced with an empty one.
func TestWebdavLockNeverEmptiesExistingFile(t *testing.T) {
	h := newDavHarness(t)
	h.drive.put("doc.txt", "precious-data")
	h.set(func(d *fakeDrive) { d.failLists = 2 })
	h.do(t, "LOCK", h.url("Ugreen Drive", "doc.txt"), lockBody, "Timeout", "Second-60")
	if got, _ := h.driveHas("doc.txt"); got != "precious-data" {
		t.Fatalf("doc.txt = %q after LOCK during a listing failure", got)
	}
	// Even with a stale "not found" (another user created it meanwhile),
	// LOCK only creates, never replaces.
	code, _ := h.do(t, "LOCK", h.url("Ugreen Drive", "new.txt"), lockBody, "Timeout", "Second-60")
	if code/100 != 2 {
		t.Fatalf("LOCK new file: %d", code)
	}
	h.drive.put("race.txt", "theirs")
	h.set(func(d *fakeDrive) { delete(d.files, "race.txt") })
	h.propfind(t, h.url("Ugreen Drive")+"/") // cache: race.txt absent
	h.drive.put("race.txt", "theirs")
	h.do(t, "LOCK", h.url("Ugreen Drive", "race.txt"), lockBody, "Timeout", "Second-60")
	if got, _ := h.driveHas("race.txt"); got != "theirs" {
		t.Fatalf("race.txt = %q, LOCK replaced a file it thought was missing", got)
	}
}

const lockBody = `<?xml version="1.0" encoding="utf-8"?><D:lockinfo xmlns:D="DAV:"><D:lockscope><D:exclusive/></D:lockscope><D:locktype><D:write/></D:locktype></D:lockinfo>`

func TestWebdavFailedCopyUploadsNothing(t *testing.T) {
	h := newDavHarness(t)
	h.drive.put("src.txt", "0123456789")
	h.drive.put("dst.txt", "keep me")
	h.set(func(d *fakeDrive) { d.truncate = map[string]int{"src.txt": 5} })
	code, _ := h.do(t, "COPY", h.url("Ugreen Drive", "src.txt"), "", "Destination", h.url("Ugreen Drive", "new.txt"))
	if code/100 == 2 {
		t.Fatalf("COPY with a broken source succeeded: %d", code)
	}
	if got, ok := h.driveHas("new.txt"); ok {
		t.Fatalf("partial copy uploaded: new.txt = %q", got)
	}
}

// An app's safe save renames its temp file over the document. If that
// upload fails the original must still be there.
func TestWebdavFailedSafeSaveKeepsOriginal(t *testing.T) {
	h := newDavHarness(t)
	h.drive.put("essay.docx", "draft 1")
	h.do(t, "PUT", h.url("Ugreen Drive", "~wrd0001.tmp"), "draft 2")
	h.set(func(d *fakeDrive) { d.failUpload = true })
	code, _ := h.do(t, "MOVE", h.url("Ugreen Drive", "~wrd0001.tmp"), "",
		"Destination", h.url("Ugreen Drive", "essay.docx"), "Overwrite", "T")
	if code/100 == 2 {
		t.Fatalf("MOVE succeeded although the upload failed: %d", code)
	}
	if got, _ := h.driveHas("essay.docx"); got != "draft 1" {
		t.Fatalf("essay.docx = %q; the original was removed before the upload", got)
	}
	if code, body := h.do(t, "GET", h.url("Ugreen Drive", "~wrd0001.tmp"), ""); code != 200 || body != "draft 2" {
		t.Fatalf("temp file lost after failed save: %d %q", code, body)
	}
}

func TestWebdavRejectsBackslashNames(t *testing.T) {
	h := newDavHarness(t)
	h.drive.dir("Q1")
	h.drive.put("Q1/notes.txt", "original")
	if code, _ := h.do(t, "PUT", h.url("Ugreen Drive", `Q1\notes.txt`), "clobber"); code/100 == 2 {
		t.Fatalf("PUT with a backslash name succeeded: %d", code)
	}
	if got, _ := h.driveHas("Q1/notes.txt"); got != "original" {
		t.Fatalf("Q1/notes.txt = %q", got)
	}
}

func TestWebdavFolderRenameKeepsLocalFiles(t *testing.T) {
	h := newDavHarness(t)
	h.drive.dir("Old")
	h.do(t, "PUT", h.url("Ugreen Drive", "Old", "._clip.mov"), "meta")
	code, _ := h.do(t, "MOVE", h.url("Ugreen Drive", "Old"), "", "Destination", h.url("Ugreen Drive", "New"))
	if code/100 != 2 {
		t.Fatalf("MOVE folder: %d", code)
	}
	if code, body := h.do(t, "GET", h.url("Ugreen Drive", "New", "._clip.mov"), ""); code != 200 || body != "meta" {
		t.Fatalf("local file after folder rename: %d %q", code, body)
	}
	h.do(t, "MKCOL", h.url("Ugreen Drive", "Old"), "")
	if code, _ := h.do(t, "GET", h.url("Ugreen Drive", "Old", "._clip.mov"), ""); code != http.StatusNotFound {
		t.Fatalf("stale local file reappeared under the old name: %d", code)
	}
}

func TestWebdavReportsUploads(t *testing.T) {
	h := newDavHarness(t)
	big := strings.Repeat("x", api.ChunkThreshold+10)
	h.do(t, "PUT", h.url("Ugreen Drive", "clip.mov"), big)
	events := h.uploadEvents()
	if len(events) < 2 {
		t.Fatalf("want start and finish events, got %+v", events)
	}
	first, last := events[0], events[len(events)-1]
	if first.State != "active" || first.Path != "Ugreen Drive/clip.mov" || first.Size != int64(len(big)) {
		t.Fatalf("first event = %+v", first)
	}
	if last.State != "done" || last.Sent != last.Size || last.ID != first.ID {
		t.Fatalf("last event = %+v", last)
	}

	h.set(func(d *fakeDrive) { d.failUpload = true })
	h.do(t, "PUT", h.url("Ugreen Drive", "note.txt"), "hello")
	events = h.uploadEvents()
	if last := events[len(events)-1]; last.State != "failed" || last.Error == "" || last.Path != "Ugreen Drive/note.txt" {
		t.Fatalf("failed upload event = %+v", last)
	}
	// Finder's empty LOCK placeholders and hidden files are not transfers.
	before := len(events)
	h.do(t, "LOCK", h.url("Ugreen Drive", "placeholder.txt"), lockBody)
	h.do(t, "PUT", h.url("Ugreen Drive", "._clip.mov"), "meta")
	if got := len(h.uploadEvents()); got != before {
		t.Fatalf("placeholder/hidden writes produced %d upload events", got-before)
	}
}

// The Mac app must not restart for an update while Finder is still writing a
// file (before its upload even starts), so the helper reports open writes.
func TestWebdavReportsOpenWrites(t *testing.T) {
	drive, driveSrv := newFakeDrive(t)
	base, _ := url.Parse(driveSrv.URL)
	fs := davfs.New(&api.Client{Base: base, Token: testToken, HTTP: driveSrv.Client()}, t.TempDir())
	var mu sync.Mutex
	var counts []int
	fs.OnWriting = func(n int) {
		mu.Lock()
		counts = append(counts, n)
		mu.Unlock()
	}
	srv := httptest.NewServer(davfs.Handler(fs, "/Ugreen Drive", davPassword, t.Logf))
	defer srv.Close()
	h := &davHarness{drive: drive, srv: srv}
	h.do(t, "PUT", h.url("Ugreen Drive", "a.txt"), "hello")
	h.do(t, "PUT", h.url("Ugreen Drive", "._a.txt"), "meta")
	mu.Lock()
	defer mu.Unlock()
	if len(counts) < 2 || counts[0] != 1 || counts[len(counts)-1] != 0 {
		t.Fatalf("open-write counts = %v, want 1 … 0", counts)
	}
}

func (h *davHarness) counts() (lists, uploads, ranges int) {
	h.drive.mu.Lock()
	defer h.drive.mu.Unlock()
	return h.drive.listCalls, h.drive.uploads, h.drive.rangeReads
}

// Finder copying small files: LOCK (new file), PUT, UNLOCK, next file. Each
// file should cost one upload, and the folder shouldn't be re-listed per file.
func TestWebdavSmallFileCopyIsOneRequestPerFile(t *testing.T) {
	h := newDavHarness(t)
	h.drive.dir("Photos")
	h.propfind(t, h.url("Ugreen Drive", "Photos")+"/") // Finder opened the folder
	lists0, uploads0, _ := h.counts()
	for i := 0; i < 5; i++ {
		name := fmt.Sprintf("img%d.jpg", i)
		code, body := h.do(t, "LOCK", h.url("Ugreen Drive", "Photos", name), lockBody, "Timeout", "Second-60")
		if code/100 != 2 {
			t.Fatalf("LOCK %s: %d", name, code)
		}
		token := lockToken(t, body)
		if code, _ := h.do(t, "PUT", h.url("Ugreen Drive", "Photos", name), "jpeg-bytes", "If", "(<"+token+">)"); code/100 != 2 {
			t.Fatalf("PUT %s: %d", name, code)
		}
		h.do(t, "UNLOCK", h.url("Ugreen Drive", "Photos", name), "", "Lock-Token", "<"+token+">")
		if code, _ := h.propfind(t, h.url("Ugreen Drive", "Photos", name)); code != http.StatusMultiStatus {
			t.Fatalf("PROPFIND %s after copy: %d", name, code)
		}
	}
	lists, uploads, _ := h.counts()
	if uploads-uploads0 != 5 {
		t.Fatalf("%d uploads for 5 files, want 5 (no empty placeholder uploads)", uploads-uploads0)
	}
	if lists-lists0 > 1 {
		t.Fatalf("%d folder listings while copying 5 files, want at most 1", lists-lists0)
	}
	for i := 0; i < 5; i++ {
		if got, _ := h.driveHas(fmt.Sprintf("Photos/img%d.jpg", i)); got != "jpeg-bytes" {
			t.Fatalf("img%d.jpg = %q", i, got)
		}
	}
}

// `touch` style: LOCK a new name, then UNLOCK without writing: the empty file
// must still end up on the Drive (once the grace period for content is over).
func TestWebdavLockThenUnlockCreatesEmptyFile(t *testing.T) {
	h := newDavHarness(t)
	h.fs.PendingGrace = 50 * time.Millisecond
	_, body := h.do(t, "LOCK", h.url("Ugreen Drive", "empty.txt"), lockBody, "Timeout", "Second-60")
	if _, ok := h.driveHas("empty.txt"); ok {
		t.Fatal("LOCK uploaded a placeholder right away")
	}
	h.do(t, "UNLOCK", h.url("Ugreen Drive", "empty.txt"), "", "Lock-Token", "<"+lockToken(t, body)+">")
	waitFor(t, "the empty file after UNLOCK", func() bool {
		got, ok := h.driveHas("empty.txt")
		return ok && got == ""
	})
}

func lockToken(t *testing.T, body string) string {
	t.Helper()
	start := strings.Index(body, "<D:href>")
	end := strings.Index(body, "</D:href>")
	if start < 0 || end < start {
		t.Fatalf("no lock token in %s", body)
	}
	return body[start+len("<D:href>") : end]
}

// Big files are read with several ranged requests in parallel (read-ahead),
// and the bytes still come out right, from the start or from an offset.
func TestWebdavLargeReadsUseParallelRanges(t *testing.T) {
	h := newDavHarness(t)
	data := make([]byte, 40<<20+12345)
	for i := range data {
		data[i] = byte(i*7 + i>>13)
	}
	h.drive.put("big.mov", string(data))
	code, body := h.do(t, "GET", h.url("Ugreen Drive", "big.mov"), "")
	if code != 200 || body != string(data) {
		t.Fatalf("GET: %d, %d bytes, equal=%v", code, len(body), body == string(data))
	}
	if _, _, ranges := h.counts(); ranges < 4 {
		t.Fatalf("%d ranged reads for a 40 MB file, want parallel chunks", ranges)
	}
	from := 17<<20 + 3
	code, body = h.do(t, "GET", h.url("Ugreen Drive", "big.mov"), "", "Range", fmt.Sprintf("bytes=%d-", from))
	if code != http.StatusPartialContent || body != string(data[from:]) {
		t.Fatalf("ranged GET: %d, %d bytes", code, len(body))
	}
}

// Someone saves a new version while this Mac copies the file out: the parallel
// read must fail rather than stitch old and new bytes together.
func TestWebdavReadAheadNeverMixesVersions(t *testing.T) {
	h := newDavHarness(t)
	old := strings.Repeat("A", 30<<20)
	h.drive.put("clip.mov", old)
	swapped := false
	h.set(func(d *fakeDrive) {
		d.onRange = func() {
			if !swapped { // runs with the drive lock held
				swapped = true
				d.clock++
				d.files["clip.mov"] = &fakeFile{data: []byte(strings.Repeat("B", 30<<20)), mtimeNS: d.clock}
			}
		}
	})
	code, body := h.do(t, "GET", h.url("Ugreen Drive", "clip.mov"), "")
	if code == 200 && strings.Contains(body, "A") && strings.Contains(body, "B") {
		t.Fatal("copy silently mixed two versions of the file")
	}
}

func TestWebdavRenamedPlaceholderBecomesEmptyFile(t *testing.T) {
	h := newDavHarness(t)
	h.drive.put("b.txt", "real document")
	h.do(t, "LOCK", h.url("Ugreen Drive", "a.txt"), lockBody, "Timeout", "Second-60")
	// What x/net/webdav does for MOVE a→b with Overwrite: T (its lock checks
	// aside): clear the destination, then rename.
	ctx := context.Background()
	if err := h.fs.RemoveAll(ctx, "/Ugreen Drive/b.txt"); err != nil {
		t.Fatal(err)
	}
	if err := h.fs.Rename(ctx, "/Ugreen Drive/a.txt", "/Ugreen Drive/b.txt"); err != nil {
		t.Fatalf("rename placeholder: %v", err)
	}
	// Like moving an empty file over b.txt: b.txt is empty on the Drive (the old
	// one went to the recycle bin), never an invisible local-only placeholder.
	if got, ok := h.driveHas("b.txt"); !ok || got != "" {
		t.Fatalf("b.txt on the Drive = %q (exists=%v)", got, ok)
	}
	if code, got := h.do(t, "GET", h.url("Ugreen Drive", "b.txt"), ""); code != 200 || got != "" {
		t.Fatalf("GET b.txt = %d %q", code, got)
	}
}

// Finder died (or the Mac slept) after LOCKing a new name: once the lock
// expires the empty file is still created.
func TestWebdavExpiredLockFinishesPlaceholder(t *testing.T) {
	h := newDavHarness(t)
	h.do(t, "LOCK", h.url("Ugreen Drive", "touched.txt"), lockBody, "Timeout", "Second-1")
	time.Sleep(1200 * time.Millisecond)
	h.do(t, "LOCK", h.url("Ugreen Drive", "other.txt"), lockBody, "Timeout", "Second-60") // any lock activity sweeps
	if got, ok := h.driveHas("touched.txt"); !ok || got != "" {
		t.Fatalf("expired placeholder: exists=%v content=%q", ok, got)
	}
}

func TestWebdavFailedPutClearsPlaceholder(t *testing.T) {
	h := newDavHarness(t)
	_, body := h.do(t, "LOCK", h.url("Ugreen Drive", "x.bin"), lockBody, "Timeout", "Second-60")
	h.set(func(d *fakeDrive) { d.failUpload = true })
	if code, _ := h.do(t, "PUT", h.url("Ugreen Drive", "x.bin"), "data", "If", "(<"+lockToken(t, body)+">)"); code/100 == 2 {
		t.Fatalf("PUT succeeded although the upload failed: %d", code)
	}
	if code, _ := h.propfind(t, h.url("Ugreen Drive", "x.bin")); code != http.StatusNotFound {
		t.Fatalf("placeholder survived a failed PUT: PROPFIND %d", code)
	}
}

func TestWebdavFailedDeleteKeepsFileListed(t *testing.T) {
	h := newDavHarness(t)
	h.drive.put("keep.txt", "x")
	h.propfind(t, h.url("Ugreen Drive")+"/")
	h.set(func(d *fakeDrive) { d.failDelete = true })
	if code, _ := h.do(t, "DELETE", h.url("Ugreen Drive", "keep.txt"), ""); code/100 == 2 {
		t.Fatalf("DELETE succeeded: %d", code)
	}
	if code, _ := h.propfind(t, h.url("Ugreen Drive", "keep.txt")); code != http.StatusMultiStatus {
		t.Fatalf("file hidden after a failed delete: %d", code)
	}
}

// A small ranged read must not start the 8 MiB read-ahead.
func TestWebdavSmallRangeReadIsOneRequest(t *testing.T) {
	h := newDavHarness(t)
	h.drive.put("big.bin", strings.Repeat("z", 30<<20))
	_, _, before := h.counts()
	code, body := h.do(t, "GET", h.url("Ugreen Drive", "big.bin"), "", "Range", "bytes=100-4195")
	if code != http.StatusPartialContent || len(body) != 4096 {
		t.Fatalf("ranged GET: %d, %d bytes", code, len(body))
	}
	if _, _, after := h.counts(); after-before > 1 {
		t.Fatalf("%d ranged requests for a 4 KB read", after-before)
	}
}

// The Mac app's speed test reads and writes in the hidden speed-test folder
// through Finder's path; nothing may land in a share.
func TestWebdavSpeedTestFolder(t *testing.T) {
	h := newDavHarness(t)
	dir := davfs.SpeedTestDir
	code, body := h.do(t, "GET", h.url(dir, "download-20-run1.bin"), "")
	if code != 200 || len(body) != 20<<20 || body[300] != byte(300%251) {
		t.Fatalf("download: %d, %d bytes", code, len(body))
	}
	if _, _, ranges := h.counts(); ranges < 2 {
		t.Fatalf("speed-test read used %d ranged requests; want the parallel path real files use", ranges)
	}
	_, lock := h.do(t, "LOCK", h.url(dir, "upload-run1.bin"), lockBody, "Timeout", "Second-60")
	payload := strings.Repeat("u", 70<<20)
	if code, _ := h.do(t, "PUT", h.url(dir, "upload-run1.bin"), payload, "If", "(<"+lockToken(t, lock)+">)"); code/100 != 2 {
		t.Fatalf("upload PUT: %d", code)
	}
	h.drive.mu.Lock()
	up, puts, uploads := h.drive.speedUp, h.drive.speedPuts, h.drive.uploads
	h.drive.mu.Unlock()
	if up != 70<<20 || puts < 3 || uploads != 0 {
		t.Fatalf("speed upload: %d bytes in %d requests (want 70 MiB in 32 MiB chunks), %d real uploads", up, puts, uploads)
	}
	if code, b := h.propfind(t, h.url(dir)+"/"); code != http.StatusMultiStatus || !strings.Contains(b, "upload-run1.bin") {
		t.Fatalf("PROPFIND speed folder: %d", code)
	}
	h.do(t, "UNLOCK", h.url(dir, "upload-run1.bin"), "", "Lock-Token", "<"+lockToken(t, lock)+">")
	if code, _ := h.do(t, "DELETE", h.url(dir, "upload-run1.bin"), ""); code != http.StatusNoContent {
		t.Fatalf("DELETE: %d", code)
	}
	if code, _ := h.propfind(t, h.url(dir, "upload-run1.bin")); code != http.StatusNotFound {
		t.Fatalf("after DELETE: %d", code)
	}
	// Listed at the top of the volume, or macOS decides the folder is gone and
	// fails every new name in it; the leading dot keeps it hidden in Finder.
	if code, b := h.propfind(t, h.url()+"/"); code != http.StatusMultiStatus ||
		!strings.Contains(b, url.PathEscape(dir)) || !strings.HasPrefix(dir, ".") {
		t.Fatalf("root listing (%d) lacks the hidden speed-test folder:\n%s", code, b)
	}
}

// On the home network the helper switches to the Drive's LAN address with
// a LAN token, and back to the internet the moment the LAN disappears.
func TestWebdavSwitchesToLANAndBack(t *testing.T) {
	drive, driveSrv := newFakeDrive(t)
	drive.put("hello.txt", "hi")
	drive.put("later.txt", "not read yet") // small reads are cached: this one isn't
	lan := drive.withLAN(t)
	dir := t.TempDir()
	if err := config.Save(dir, config.Config{Server: driveSrv.URL}); err != nil {
		t.Fatal(err)
	}
	stdinR, stdinW := io.Pipe()
	stdoutR, stdoutW := io.Pipe()
	var stderr bytes.Buffer
	env := Env{
		Stdin: stdinR, Stdout: stdoutW, Stderr: &stderr, ConfigDir: dir,
		Tokens: config.MemoryStore{strings.TrimPrefix(driveSrv.URL, "http://"): testToken},
		HTTP:   &http.Client{}, Getenv: func(string) string { return "" },
	}
	go func() { Run(context.Background(), []string{"webdav"}, env); stdoutW.Close() }()
	io.WriteString(stdinW, davPassword+"\n")
	events := make(chan map[string]any, 64)
	go func() {
		scanner := bufio.NewScanner(stdoutR)
		for scanner.Scan() {
			var ev map[string]any
			if json.Unmarshal(scanner.Bytes(), &ev) == nil {
				events <- ev
			}
		}
	}()
	waitFor := func(event, key, value string) map[string]any {
		t.Helper()
		deadline := time.After(10 * time.Second)
		for {
			select {
			case ev := <-events:
				if ev["event"] == event && (key == "" || ev[key] == value) {
					return ev
				}
			case <-deadline:
				t.Fatalf("no %s %s=%s event; stderr: %s", event, key, value, stderr.String())
			}
		}
	}
	ready := waitFor("ready", "", "")
	waitFor("route", "via", "lan")
	// The app shows the round trip Finder's requests actually take.
	if ev := waitFor("latency", "via", "lan"); ev["ms"] == nil {
		t.Fatalf("latency event without ms: %v", ev)
	}
	client := &http.Client{Transport: &http.Transport{DisableKeepAlives: true}}
	get := func(name ...string) string {
		file := "hello.txt"
		if len(name) > 0 {
			file = name[0]
		}
		req, _ := http.NewRequest("GET", ready["url"].(string)+"Ugreen%20Drive/"+file, nil)
		req.SetBasicAuth(davfs.User, davPassword)
		res, err := client.Do(req)
		if err != nil {
			t.Fatal(err)
		}
		defer res.Body.Close()
		body, _ := io.ReadAll(res.Body)
		return string(body)
	}
	drive.mu.Lock()
	before := drive.lanHits
	drive.mu.Unlock()
	if got := get(); got != "hi" {
		t.Fatalf("read over LAN = %q", got)
	}
	drive.mu.Lock()
	viaLAN := drive.lanHits > before
	drive.mu.Unlock()
	if !viaLAN {
		t.Fatal("read didn't use the LAN route")
	}
	lan.CloseClientConnections()
	lan.Close() // walked out of the building
	if got := get("later.txt"); got != "not read yet" {
		t.Fatalf("read after leaving the LAN = %q (want a seamless fallback)", got)
	}
	waitFor("route", "via", "internet")
	stdinW.Close()
}

// waitFor polls cond for up to two seconds.
func waitFor(t *testing.T, what string, cond func() bool) {
	t.Helper()
	for deadline := time.Now().Add(2 * time.Second); time.Now().Before(deadline); time.Sleep(10 * time.Millisecond) {
		if cond() {
			return
		}
	}
	t.Fatalf("timed out waiting for %s", what)
}

// What macOS sends for each new file: an empty PUT to create it, a LOCK and
// UNLOCK, then LOCK, the real PUT and UNLOCK. That must be one upload, not an
// empty file first and the content second.
func TestWebdavFinderNewFileIsOneUpload(t *testing.T) {
	h := newDavHarness(t)
	h.fs.PendingGrace = 50 * time.Millisecond
	h.drive.dir("Photos")
	h.propfind(t, h.url("Ugreen Drive", "Photos")+"/")
	_, uploads0, _ := h.counts()
	for i := 0; i < 3; i++ {
		file := h.url("Ugreen Drive", "Photos", fmt.Sprintf("img%d.jpg", i))
		if code, _ := h.propfind(t, file); code != http.StatusNotFound {
			t.Fatalf("PROPFIND before create: %d", code)
		}
		if code, _ := h.do(t, "PUT", file, ""); code/100 != 2 {
			t.Fatalf("empty PUT: %d", code)
		}
		if code, _ := h.propfind(t, file); code != http.StatusMultiStatus {
			t.Fatalf("new file not visible after the empty PUT: %d", code)
		}
		_, body := h.do(t, "LOCK", file, lockBody, "Timeout", "Second-60")
		h.do(t, "UNLOCK", file, "", "Lock-Token", "<"+lockToken(t, body)+">")
		_, body = h.do(t, "LOCK", file, lockBody, "Timeout", "Second-60")
		token := lockToken(t, body)
		if code, _ := h.do(t, "PUT", file, "jpeg-bytes", "If", "(<"+token+">)"); code/100 != 2 {
			t.Fatalf("PUT content: %d", code)
		}
		h.do(t, "UNLOCK", file, "", "Lock-Token", "<"+token+">")
	}
	time.Sleep(150 * time.Millisecond) // past the grace period: no late empty uploads
	if _, uploads, _ := h.counts(); uploads-uploads0 != 3 {
		t.Fatalf("%d uploads for 3 new files, want 3", uploads-uploads0)
	}
	for i := 0; i < 3; i++ {
		if got, _ := h.driveHas(fmt.Sprintf("Photos/img%d.jpg", i)); got != "jpeg-bytes" {
			t.Fatalf("img%d.jpg = %q", i, got)
		}
	}
}

// `touch new.txt`: an empty PUT and nothing else still creates the file.
func TestWebdavEmptyPutCreatesFileAfterGrace(t *testing.T) {
	h := newDavHarness(t)
	h.fs.PendingGrace = 50 * time.Millisecond
	if code, _ := h.do(t, "PUT", h.url("Ugreen Drive", "new.txt"), ""); code/100 != 2 {
		t.Fatalf("empty PUT: %d", code)
	}
	waitFor(t, "the empty file on the Drive", func() bool {
		got, ok := h.driveHas("new.txt")
		return ok && got == ""
	})
}

// Emptying a file that exists is a real change: it uploads right away.
func TestWebdavEmptyPutOverExistingFileUploadsNow(t *testing.T) {
	h := newDavHarness(t)
	h.fs.PendingGrace = time.Hour
	h.drive.put("notes.txt", "old text")
	if code, _ := h.do(t, "PUT", h.url("Ugreen Drive", "notes.txt"), ""); code/100 != 2 {
		t.Fatalf("PUT: %d", code)
	}
	if got, _ := h.driveHas("notes.txt"); got != "" {
		t.Fatalf("notes.txt = %q, want emptied", got)
	}
}

// Quitting while a new file waits out its grace period still creates it.
func TestWebdavShutdownCreatesWaitingFiles(t *testing.T) {
	h := newDavHarness(t)
	h.fs.PendingGrace = time.Hour
	h.do(t, "PUT", h.url("Ugreen Drive", "a.txt"), "")
	_, body := h.do(t, "LOCK", h.url("Ugreen Drive", "b.txt"), lockBody, "Timeout", "Second-60")
	h.do(t, "UNLOCK", h.url("Ugreen Drive", "b.txt"), "", "Lock-Token", "<"+lockToken(t, body)+">")
	if _, ok := h.driveHas("a.txt"); ok {
		t.Fatal("created before the grace period ended")
	}
	h.fs.FinishPending()
	for _, name := range []string{"a.txt", "b.txt"} {
		if got, ok := h.driveHas(name); !ok || got != "" {
			t.Fatalf("%s after shutdown: exists=%v content=%q", name, ok, got)
		}
	}
}

// macOS reads 4 MB ranges while it downloads a file; each must fetch just
// those bytes from the Drive, not start a read-ahead to the end of the file.
func TestWebdavRangedReadFetchesOnlyThatRange(t *testing.T) {
	h := newDavHarness(t)
	data := make([]byte, 64<<20)
	for i := range data {
		data[i] = byte(i * 13)
	}
	h.drive.put("film.mov", string(data))
	for _, rng := range [][2]int{{8 << 20, 12<<20 - 1}, {0, 65535}, {100, 4195}} {
		h.set(func(d *fakeDrive) { d.rangeBytes = 0 })
		code, body := h.do(t, "GET", h.url("Ugreen Drive", "film.mov"), "", "Range", fmt.Sprintf("bytes=%d-%d", rng[0], rng[1]))
		want := string(data[rng[0] : rng[1]+1])
		if code != http.StatusPartialContent || body != want {
			t.Fatalf("GET %v: %d, %d bytes, equal=%v", rng, code, len(body), body == want)
		}
		h.drive.mu.Lock()
		fetched := h.drive.rangeBytes
		h.drive.mu.Unlock()
		// Small reads fetch the 1 MiB blocks around them (then cached); never
		// a read-ahead past those blocks.
		limit := int64(len(want))
		if limit <= 2<<20 {
			limit = (int64(rng[1])/(1<<20) - int64(rng[0])/(1<<20) + 1) << 20
		}
		if fetched > limit {
			t.Fatalf("GET %v fetched %d bytes from the Drive, want at most %d", rng, fetched, limit)
		}
	}
}

// A 20 MB file (a photo burst, a short clip) goes up as several chunks in
// parallel, not as one 32 MiB chunk on a single stream.
func TestWebdavMidSizeUploadUsesParallelChunks(t *testing.T) {
	h := newDavHarness(t)
	data := strings.Repeat("m", 20<<20)
	if code, _ := h.do(t, "PUT", h.url("Ugreen Drive", "clip.mov"), data); code != http.StatusCreated {
		t.Fatalf("PUT: %d", code)
	}
	if got, _ := h.driveHas("clip.mov"); got != data {
		t.Fatalf("clip.mov has %d bytes", len(got))
	}
	h.drive.mu.Lock()
	pieces := h.drive.chunkPuts
	h.drive.mu.Unlock()
	if pieces < 4 {
		t.Fatalf("20 MB went up in %d chunks, want at least 4 for parallel streams", pieces)
	}
}

// A new empty file waiting for its content moves with its folder.
func TestWebdavWaitingFileFollowsFolderMove(t *testing.T) {
	h := newDavHarness(t)
	h.fs.PendingGrace = 50 * time.Millisecond
	h.drive.dir("Docs")
	if code, _ := h.do(t, "PUT", h.url("Ugreen Drive", "Docs", "new.txt"), ""); code/100 != 2 {
		t.Fatalf("PUT: %d", code)
	}
	if code, _ := h.do(t, "MOVE", h.url("Ugreen Drive", "Docs")+"/", "", "Destination", h.url("Ugreen Drive", "Docs2")+"/"); code/100 != 2 {
		t.Fatalf("MOVE: %d", code)
	}
	waitFor(t, "Docs2/new.txt on the Drive", func() bool {
		got, ok := h.driveHas("Docs2/new.txt")
		return ok && got == ""
	})
}

// If creating a waiting empty file fails, the app hears about it.
func TestWebdavFailedEmptyCreateIsReported(t *testing.T) {
	h := newDavHarness(t)
	h.fs.PendingGrace = 50 * time.Millisecond
	h.set(func(d *fakeDrive) { d.failUpload = true })
	h.do(t, "PUT", h.url("Ugreen Drive", "touched.txt"), "")
	waitFor(t, "a failed upload event", func() bool {
		for _, u := range h.uploadEvents() {
			if u.State == "failed" && strings.HasSuffix(u.Path, "touched.txt") {
				return true
			}
		}
		return false
	})
}

// A new file's content taking longer to upload than the grace period must not
// also create the empty file meanwhile.
func TestWebdavSlowUploadCreatesNoEmptyFileMidway(t *testing.T) {
	h := newDavHarness(t)
	h.fs.PendingGrace = 50 * time.Millisecond
	file := h.url("Ugreen Drive", "slow.mov")
	h.do(t, "PUT", file, "")
	h.set(func(d *fakeDrive) { d.upDelay = 300 * time.Millisecond })
	_, uploads0, _ := h.counts()
	movie := strings.Repeat("m", api.ChunkThreshold+1) // chunked, so slow
	if code, _ := h.do(t, "PUT", file, movie); code/100 != 2 {
		t.Fatalf("PUT: %d", code)
	}
	time.Sleep(100 * time.Millisecond)
	if _, uploads, _ := h.counts(); uploads-uploads0 != 1 {
		t.Fatalf("%d uploads for one file, want 1 (no empty file while the content uploaded)", uploads-uploads0)
	}
	if got, _ := h.driveHas("slow.mov"); got != movie {
		t.Fatalf("slow.mov has %d bytes", len(got))
	}
}
