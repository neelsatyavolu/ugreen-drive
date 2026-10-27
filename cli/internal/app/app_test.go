package app

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"io"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"testing"

	"github.com/neelsatyavolu/ugreen-drive/cli/internal/config"
)

type harness struct {
	drive  *fakeDrive
	env    Env
	stdout *bytes.Buffer
	stderr *bytes.Buffer
	tokens config.MemoryStore
}

func newHarness(t *testing.T) *harness {
	t.Helper()
	drive, srv := newFakeDrive(t)
	dir := t.TempDir()
	if err := config.Save(dir, config.Config{Server: srv.URL}); err != nil {
		t.Fatal(err)
	}
	host := strings.TrimPrefix(srv.URL, "http://")
	h := &harness{drive: drive, stdout: &bytes.Buffer{}, stderr: &bytes.Buffer{},
		tokens: config.MemoryStore{host: testToken}}
	h.env = Env{
		Stdin: strings.NewReader(""), Stdout: h.stdout, Stderr: h.stderr,
		ConfigDir: dir, Tokens: h.tokens, HTTP: srv.Client(),
		RunEditor:  func(string) error { return nil },
		DeviceName: func() string { return "Test Mac" },
		Getenv:     func(string) string { return "" },
	}
	return h
}

func (h *harness) run(args ...string) int {
	h.stdout.Reset()
	h.stderr.Reset()
	return Run(context.Background(), args, h.env)
}

func (h *harness) mustRun(t *testing.T, args ...string) string {
	t.Helper()
	if code := h.run(args...); code != ExitOK {
		t.Fatalf("ugd %v: exit %d, stderr: %s", args, code, h.stderr)
	}
	return h.stdout.String()
}

func TestLsJSON(t *testing.T) {
	h := newHarness(t)
	h.drive.dir("Documents")
	h.drive.put("Documents/notes.md", "hello")
	var listing struct {
		Items []struct {
			Name    string `json:"name"`
			MtimeNS int64  `json:"mtime_ns"`
		} `json:"items"`
	}
	if err := json.Unmarshal([]byte(h.mustRun(t, "--json", "ls", "/Documents/")), &listing); err != nil {
		t.Fatal(err)
	}
	if len(listing.Items) != 1 || listing.Items[0].Name != "notes.md" || listing.Items[0].MtimeNS == 0 {
		t.Fatalf("got %+v", listing)
	}
}

func TestCat(t *testing.T) {
	h := newHarness(t)
	h.drive.put("a.txt", "contents")
	if out := h.mustRun(t, "cat", "a.txt"); out != "contents" {
		t.Fatalf("cat = %q", out)
	}
}

func TestPutRefusesOverwriteWithoutForce(t *testing.T) {
	h := newHarness(t)
	h.drive.put("a.txt", "old")
	local := filepath.Join(t.TempDir(), "a.txt")
	os.WriteFile(local, []byte("new"), 0o600)
	if code := h.run("put", local, "a.txt"); code != ExitConflict {
		t.Fatalf("exit %d, want %d; stderr %s", code, ExitConflict, h.stderr)
	}
	if !strings.Contains(h.stderr.String(), "--force") {
		t.Fatalf("hint missing: %s", h.stderr)
	}
	h.mustRun(t, "put", "--force", local, "a.txt")
	if got := string(h.drive.files["a.txt"].data); got != "new" {
		t.Fatalf("drive has %q", got)
	}
}

func TestPutIntoFolderAndFromStdin(t *testing.T) {
	h := newHarness(t)
	h.drive.dir("Docs")
	local := filepath.Join(t.TempDir(), "report.txt")
	os.WriteFile(local, []byte("r"), 0o600)
	h.mustRun(t, "put", local, "Docs")
	if _, ok := h.drive.files["Docs/report.txt"]; !ok {
		t.Fatal("expected Docs/report.txt")
	}
	h.env.Stdin = strings.NewReader("from stdin")
	h.mustRun(t, "put", "-", "Docs/piped.txt")
	if got := string(h.drive.files["Docs/piped.txt"].data); got != "from stdin" {
		t.Fatalf("got %q", got)
	}
}

func TestPutExpectMtimeDetectsConcurrentChange(t *testing.T) {
	h := newHarness(t)
	h.drive.put("a.txt", "v1")
	seen := h.drive.files["a.txt"].mtimeNS
	h.drive.put("a.txt", "someone else")
	local := filepath.Join(t.TempDir(), "a.txt")
	os.WriteFile(local, []byte("mine"), 0o600)
	if code := h.run("put", "--expect-mtime-ns", strconv.FormatInt(seen, 10), local, "a.txt"); code != ExitConflict {
		t.Fatalf("exit %d", code)
	}
	if got := string(h.drive.files["a.txt"].data); got != "someone else" {
		t.Fatalf("overwrote: %q", got)
	}
}

func TestChunkedUploadRetriesAFailedPiece(t *testing.T) {
	h := newHarness(t)
	big := bytes.Repeat([]byte("x"), 40<<20) // 2 chunks of 32 MiB
	local := filepath.Join(t.TempDir(), "big.bin")
	os.WriteFile(local, big, 0o600)
	h.drive.failOnce[1] = true
	h.mustRun(t, "put", local, "big.bin")
	if got := h.drive.files["big.bin"]; got == nil || !bytes.Equal(got.data, big) {
		t.Fatal("chunked upload content mismatch")
	}
}

func TestEditSavesWhenUnchangedRemotely(t *testing.T) {
	h := newHarness(t)
	h.drive.put("notes.md", "draft")
	h.env.StdinIsTTY = true
	h.env.RunEditor = func(p string) error { return os.WriteFile(p, []byte("final"), 0o600) }
	h.mustRun(t, "edit", "notes.md")
	if got := string(h.drive.files["notes.md"].data); got != "final" {
		t.Fatalf("got %q", got)
	}
}

func TestEditConflictKeepsLocalCopy(t *testing.T) {
	h := newHarness(t)
	h.drive.put("notes.md", "draft")
	h.env.StdinIsTTY = true
	h.env.RunEditor = func(p string) error {
		h.drive.mu.Lock()
		h.drive.put("notes.md", "teammate edit")
		h.drive.mu.Unlock()
		return os.WriteFile(p, []byte("mine"), 0o600)
	}
	if code := h.run("edit", "notes.md"); code != ExitConflict {
		t.Fatalf("exit %d, stderr %s", code, h.stderr)
	}
	if got := string(h.drive.files["notes.md"].data); got != "teammate edit" {
		t.Fatalf("overwrote teammate: %q", got)
	}
	msg := h.stderr.String()
	start := strings.Index(msg, "saved at ")
	if start < 0 {
		t.Fatalf("no local path in %q", msg)
	}
	local := strings.TrimSpace(msg[start+len("saved at "):])
	if data, err := os.ReadFile(local); err != nil || string(data) != "mine" {
		t.Fatalf("local copy %q: %v", data, err)
	}
	os.RemoveAll(filepath.Dir(local))
}

func TestEditNeedsTTY(t *testing.T) {
	h := newHarness(t)
	h.drive.put("notes.md", "draft")
	if code := h.run("edit", "notes.md"); code != ExitUsage {
		t.Fatalf("exit %d", code)
	}
}

func TestMkdirParentsIsIdempotent(t *testing.T) {
	h := newHarness(t)
	h.mustRun(t, "mkdir", "-p", "A/B/C")
	h.mustRun(t, "mkdir", "A/B/C", "-p")
	if f := h.drive.files["A/B/C"]; f == nil || !f.isDir {
		t.Fatal("A/B/C not created")
	}
	if code := h.run("mkdir", "A"); code != ExitConflict {
		t.Fatalf("mkdir existing without -p: exit %d", code)
	}
}

func TestRmMvGet(t *testing.T) {
	h := newHarness(t)
	h.drive.dir("Archive")
	h.drive.put("a.txt", "a")
	h.drive.put("b.txt", "b")
	h.mustRun(t, "mv", "a.txt", "Archive")
	if _, ok := h.drive.files["Archive/a.txt"]; !ok {
		t.Fatal("move failed")
	}
	out := h.mustRun(t, "rm", "b.txt") // stdin not a TTY: no prompt
	if !strings.Contains(out, "Recycle") {
		t.Fatalf("rm output %q", out)
	}
	dest := filepath.Join(t.TempDir(), "copy.txt")
	h.mustRun(t, "get", "Archive/a.txt", dest)
	if data, _ := os.ReadFile(dest); string(data) != "a" {
		t.Fatalf("get wrote %q", data)
	}
	if code := h.run("get", "Archive/a.txt", dest); code != ExitConflict {
		t.Fatalf("get over existing file: exit %d", code)
	}
	if code := h.run("rm", ""); code != ExitUsage {
		t.Fatalf("rm share root: exit %d", code)
	}
}

func TestShareFlagAndShareUse(t *testing.T) {
	h := newHarness(t)
	h.mustRun(t, "ls", "--share", "Photos")
	if h.drive.lastShare != "Photos" {
		t.Fatalf("share header %q", h.drive.lastShare)
	}
	if code := h.run("share", "use", "Nope"); code != ExitNotFound {
		t.Fatalf("exit %d", code)
	}
	h.mustRun(t, "share", "use", "Photos")
	h.mustRun(t, "ls")
	if h.drive.lastShare != "Photos" {
		t.Fatalf("saved share not sent: %q", h.drive.lastShare)
	}
}

func TestAuthErrorsExit3(t *testing.T) {
	h := newHarness(t)
	for host := range h.tokens {
		h.tokens[host] = "ugd_revoked"
	}
	if code := h.run("--json", "ls"); code != ExitAuth {
		t.Fatalf("revoked token: exit %d", code)
	}
	var errOut map[string]any
	if err := json.Unmarshal(h.stderr.Bytes(), &errOut); err != nil || errOut["status"] != float64(401) {
		t.Fatalf("json error %q: %v", h.stderr, err)
	}
	for host := range h.tokens {
		delete(h.tokens, host)
	}
	if code := h.run("whoami"); code != ExitAuth || !strings.Contains(h.stderr.String(), "ugd login") {
		t.Fatalf("no token: exit %d %s", code, h.stderr)
	}
}

func TestLogoutRevokesAndForgets(t *testing.T) {
	h := newHarness(t)
	h.mustRun(t, "logout")
	if !h.drive.loggedOut || len(h.tokens) != 0 {
		t.Fatalf("loggedOut=%v tokens=%v", h.drive.loggedOut, h.tokens)
	}
}

func TestUsageErrors(t *testing.T) {
	h := newHarness(t)
	for _, args := range [][]string{{"bogus"}, {"cat"}, {"put", "only-one"}, {"--share"}} {
		if code := h.run(args...); code != ExitUsage {
			t.Errorf("%v: exit %d", args, code)
		}
	}
	if !strings.Contains(h.mustRun(t, "help", "agents"), "Exit codes") {
		t.Error("agent guide missing exit codes")
	}
}

func TestMissingServerIsUsageError(t *testing.T) {
	h := newHarness(t)
	h.env.ConfigDir = t.TempDir()
	if code := h.run("ls"); code != ExitUsage {
		t.Fatalf("exit %d", code)
	}
}

// appearingReader creates path the first time it is read, like another
// process writing the destination while our download is in flight.
type appearingReader struct {
	path string
	done bool
}

func (r *appearingReader) Read(p []byte) (int, error) {
	if r.done {
		return 0, io.EOF
	}
	r.done = true
	os.WriteFile(r.path, []byte("theirs"), 0o600)
	return copy(p, "mine"), nil
}

func TestWriteLocalNeverReplacesAFileThatAppearsMidDownload(t *testing.T) {
	dest := filepath.Join(t.TempDir(), "out.txt")
	_, err := writeLocal(dest, &appearingReader{path: dest}, false)
	var exit exitError
	if !errors.As(err, &exit) || exit.code != ExitConflict {
		t.Fatalf("want conflict, got %v", err)
	}
	if data, _ := os.ReadFile(dest); string(data) != "theirs" {
		t.Fatalf("destination replaced: %q", data)
	}
	if leftovers, _ := filepath.Glob(filepath.Join(filepath.Dir(dest), ".ugd-*")); len(leftovers) != 0 {
		t.Fatalf("temp files left: %v", leftovers)
	}
}

func TestWhoamiAndSharesUnderstandShareObjects(t *testing.T) {
	h := newHarness(t)
	if out := h.mustRun(t, "whoami"); !strings.Contains(out, "alex") {
		t.Fatalf("whoami: %q", out)
	}
	out := h.mustRun(t, "shares")
	for _, want := range []string{"* Ugreen Drive", "Photos", "~alex", "My folder"} {
		if !strings.Contains(out, want) {
			t.Fatalf("shares output missing %q:\n%s", want, out)
		}
	}
	h.mustRun(t, "share", "use", "My folder") // by display name
	h.mustRun(t, "ls")
	if h.drive.lastShare != "~alex" {
		t.Fatalf("share header %q, want the id ~alex", h.drive.lastShare)
	}
}

func TestMoveServerKeepsTheSignIn(t *testing.T) {
	h := newHarness(t)
	h.mustRun(t, "move-server", "http://localhost:4321")
	if h.tokens["localhost:4321"] != testToken || len(h.tokens) != 1 {
		t.Fatalf("tokens=%v", h.tokens)
	}
	cfg, err := config.Load(h.env.ConfigDir)
	if err != nil || cfg.Server != "http://localhost:4321" {
		t.Fatalf("server=%q err=%v", cfg.Server, err)
	}
	if code := h.run("move-server"); code != ExitUsage {
		t.Fatalf("no URL: exit %d", code)
	}
}
