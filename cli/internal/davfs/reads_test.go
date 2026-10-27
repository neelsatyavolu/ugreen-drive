package davfs

import (
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
)

// revDrive serves one file in share S, whose content and version tests change.
type revDrive struct {
	mu         sync.Mutex
	name       string
	data       []byte
	etag       string
	swapAt     int64 // a range starting here is served from data2
	data2      []byte
	rangeBytes int64
	ranges     []string
	mtime      int64
	inflight   int // ranged downloads being served right now
	maxFlight  int
}

func newRevDrive(t *testing.T, name string, data []byte) (*revDrive, *FS, *httptest.Server) {
	d := &revDrive{name: name, data: data, etag: `"v1"`, swapAt: -1}
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		q := r.URL.Query()
		switch r.URL.Path {
		case "/api/me":
			json.NewEncoder(w).Encode(map[string]any{"authenticated": true,
				"shares": []map[string]any{{"id": "S", "name": "S", "can_read": true, "can_write": true}}})
		case "/api/files":
			d.mu.Lock()
			items := []api.Entry{{Name: d.name, Path: d.name, Size: int64(len(d.data)), MtimeNS: d.mtime}}
			d.mu.Unlock()
			json.NewEncoder(w).Encode(map[string]any{"path": q.Get("path"), "items": items})
		case "/api/download":
			d.mu.Lock()
			d.inflight++
			d.maxFlight = max(d.maxFlight, d.inflight)
			d.mu.Unlock()
			defer func() { d.mu.Lock(); d.inflight--; d.mu.Unlock() }()
			time.Sleep(5 * time.Millisecond) // a real link: requests overlap
			d.mu.Lock()
			data, etag := d.data, d.etag
			if rng := r.Header.Get("Range"); rng != "" {
				d.ranges = append(d.ranges, rng)
				var a, b int64
				fmt.Sscanf(rng, "bytes=%d-%d", &a, &b)
				d.rangeBytes += b - a + 1
				if a == d.swapAt {
					data, etag = d.data2, `"v2"`
				}
			}
			d.mu.Unlock()
			w.Header().Set("ETag", etag)
			http.ServeContent(w, r, "", time.Time{}, bytes.NewReader(data))
		default:
			http.NotFound(w, r)
		}
	}))
	t.Cleanup(srv.Close)
	base, _ := url.Parse(srv.URL)
	fs := New(&api.Client{Base: base, Token: "ugd_test", HTTP: srv.Client()}, t.TempDir())
	dav := httptest.NewServer(Handler(fs, "/V", "pw", t.Logf))
	t.Cleanup(dav.Close)
	return d, fs, dav
}

func davDo(t *testing.T, dav *httptest.Server, method, p string, hdr ...string) (int, []byte) {
	req, _ := http.NewRequest(method, dav.URL+"/V/S/"+p, nil)
	req.SetBasicAuth(User, "pw")
	for i := 0; i+1 < len(hdr); i += 2 {
		req.Header.Set(hdr[i], hdr[i+1])
	}
	res, err := dav.Client().Do(req)
	if err != nil {
		t.Fatal(err)
	}
	defer res.Body.Close()
	b, _ := io.ReadAll(res.Body)
	return res.StatusCode, b
}

// A listing that joins a fetch started before an upload finished still shows
// the uploaded file.
func TestJoinedListingIncludesLocalChange(t *testing.T) {
	d, fs := newSlowDrive(t)
	ctx := context.Background()
	if _, err := fs.Stat(ctx, "/S"); err != nil {
		t.Fatal(err)
	}
	d.listDelay.Store(int64(300 * time.Millisecond))
	go fs.Stat(ctx, "/S/Documents/a.mov") // starts a fetch of Documents
	eventually(t, "the fetch to start", func() bool { return d.listCalls.Load() >= 1 })
	tgt, err := fs.resolve(ctx, "/S/Documents/b.mov")
	if err != nil {
		t.Fatal(err)
	}
	fs.cachePut(tgt, api.Entry{Name: "b.mov", Path: "Documents/b.mov", Size: 5}) // the upload finished
	if _, err := fs.Stat(ctx, "/S/Documents/b.mov"); err != nil {
		t.Fatalf("just-uploaded file not found: %v", err)
	}
}

// Copying into a folder while Finder keeps listing it: every file is found
// right after its upload, and listings still get cached (not one per file).
func TestCopyIntoListedFolderFindsEveryFile(t *testing.T) {
	d, fs := newSlowDrive(t)
	fs.listTTL, fs.listStale = 30*time.Millisecond, 60*time.Millisecond
	ctx := context.Background()
	if _, err := fs.Stat(ctx, "/S/Documents/a.mov"); err != nil {
		t.Fatal(err)
	}
	d.listDelay.Store(int64(10 * time.Millisecond))
	tgt, _ := fs.resolve(ctx, "/S/Documents/x")
	stop := make(chan struct{})
	done := make(chan struct{})
	go func() { // Finder showing the folder
		defer close(done)
		for {
			select {
			case <-stop:
				return
			default:
			}
			fs.readDir(ctx, node{t: tgt.with("Documents")})
			time.Sleep(time.Millisecond)
		}
	}()
	before := d.listCalls.Load()
	missing := 0
	for i := 0; i < 60; i++ {
		e := api.Entry{Name: fmt.Sprintf("f%d", i), Path: fmt.Sprintf("Documents/f%d", i)}
		d.mu.Lock()
		d.items = append(d.items, e)
		d.mu.Unlock()
		fs.cachePut(tgt.with(e.Path), e) // upload done
		if _, err := fs.Stat(ctx, "/S/"+e.Path); err != nil {
			missing++
		}
		time.Sleep(3 * time.Millisecond)
	}
	close(stop)
	<-done
	if missing > 0 {
		t.Fatalf("%d of 60 just-uploaded files were not found right after their upload", missing)
	}
	// ~200 ms of copying with a 30 ms TTL: a handful of listings, not one per file.
	if n := d.listCalls.Load() - before; n > 20 {
		t.Fatalf("%d listings while copying 60 files", n)
	}
}

// The file grew on the Drive after Finder listed the folder: opening it uses
// a fresh listing, so the whole new file is served.
func TestOpenNeverUsesAStaleSize(t *testing.T) {
	d, fs, dav := newRevDrive(t, "f.png", bytes.Repeat([]byte("A"), 1000))
	fs.listTTL = 10 * time.Millisecond
	if code, _ := davDo(t, dav, "PROPFIND", "", "Depth", "1"); code != 207 {
		t.Fatalf("PROPFIND: %d", code)
	}
	d.mu.Lock()
	d.data, d.etag = bytes.Repeat([]byte("B"), 3000), `"v2"`
	d.mu.Unlock()
	time.Sleep(30 * time.Millisecond)
	if code, body := davDo(t, dav, "GET", "f.png"); code != 200 || !bytes.Equal(body, bytes.Repeat([]byte("B"), 3000)) {
		t.Fatalf("GET: %d, %d bytes", code, len(body))
	}
}

// Even a fresh listing can be out of date: a read whose download names
// another size fails rather than serve a cut-off file, and the next read
// re-lists.
func TestSizeMismatchFailsTheReadAndRelists(t *testing.T) {
	d, fs, dav := newRevDrive(t, "f.png", bytes.Repeat([]byte("A"), 1000))
	fs.listTTL = time.Hour
	davDo(t, dav, "PROPFIND", "", "Depth", "1")
	d.mu.Lock()
	d.data, d.etag = bytes.Repeat([]byte("B"), 3000), `"v2"`
	d.mu.Unlock()
	if code, body := davDo(t, dav, "GET", "f.png"); code == 200 && len(body) == 1000 {
		t.Fatal("served 1000 bytes of a 3000-byte file without error")
	}
	if code, body := davDo(t, dav, "GET", "f.png"); code != 200 || len(body) != 3000 {
		t.Fatalf("GET after the mismatch: %d, %d bytes", code, len(body))
	}
}

// A multi-range GET reads each part separately; all must be one version.
func TestMultiRangeNeverMixesVersions(t *testing.T) {
	size := 10 << 20
	d, _, dav := newRevDrive(t, "f.png", bytes.Repeat([]byte("A"), size))
	d.data2 = bytes.Repeat([]byte("B"), size)
	d.swapAt = 9000000
	code, body := davDo(t, dav, "GET", "f.png", "Range", "bytes=0-5242879,9000000-9000099")
	if code == 206 && bytes.Contains(body, []byte("AAAA")) && bytes.Contains(body, []byte("BBBB")) {
		t.Fatal("multi-range response mixed two versions")
	}
}

// A file without an extension: serving it must not read its start to guess
// the type (a Drive request of its own, then abandoned).
func TestUnknownTypeFetchesOnlyTheRange(t *testing.T) {
	d, _, dav := newRevDrive(t, "clip", make([]byte, 64<<20))
	code, body := davDo(t, dav, "GET", "clip", "Range", "bytes=40000000-40065535")
	if code != http.StatusPartialContent || len(body) != 65536 {
		t.Fatalf("ranged GET: %d, %d bytes", code, len(body))
	}
	if code, _ := davDo(t, dav, "HEAD", "clip"); code != 200 {
		t.Fatalf("HEAD: %d", code)
	}
	time.Sleep(50 * time.Millisecond)
	d.mu.Lock()
	defer d.mu.Unlock()
	// One request for the 1 MiB block holding the range (small reads are
	// cached by block), nothing for HEAD, no read-ahead.
	if d.rangeBytes != blockSize || len(d.ranges) != 1 {
		t.Fatalf("Drive asked for %d bytes in %v", d.rangeBytes, d.ranges)
	}
}

// Finder making a thumbnail opens a big video and reads a little of it. The
// read must not start fetching megabytes ahead it will never use.
func TestPeekingAtABigFileFetchesLittle(t *testing.T) {
	d, _, dav := newRevDrive(t, "clip.bin", make([]byte, 64<<20)) // not a video: no index prefetch
	req, _ := http.NewRequest("GET", dav.URL+"/V/S/clip.bin", nil)
	req.SetBasicAuth(User, "pw")
	res, err := dav.Client().Do(req)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := io.ReadFull(res.Body, make([]byte, 300<<10)); err != nil {
		t.Fatal(err)
	}
	time.Sleep(100 * time.Millisecond) // let anything already started finish
	res.Body.Close()
	d.mu.Lock()
	defer d.mu.Unlock()
	// Socket buffers take ~1 MB more than the client reads; before the ramp
	// this fetched 17+ MB (six chunks at once).
	if d.rangeBytes > 6<<20 {
		t.Fatalf("reading 300 KB fetched %d MB from the Drive: %v", d.rangeBytes>>20, d.ranges)
	}
}

// Many files read at once (a folder of thumbnails) share a cap on parallel
// downloads, so they can't swamp the link and stall everything else.
func TestParallelReadsShareACap(t *testing.T) {
	d, _, dav := newRevDrive(t, "clip.mov", make([]byte, 48<<20))
	var wg sync.WaitGroup
	for i := 0; i < 5; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			if code, body := davDo(t, dav, "GET", "clip.mov"); code != 200 || len(body) != 48<<20 {
				t.Errorf("GET: %d, %d bytes", code, len(body))
			}
		}()
	}
	wg.Wait()
	d.mu.Lock()
	defer d.mu.Unlock()
	// totalReadStreams large chunks, plus each read's small first chunk.
	if d.maxFlight > totalReadStreams+5 {
		t.Fatalf("%d downloads at once for 5 reads", d.maxFlight)
	}
}

// quickLookReads is what Quick Look asks for to make one video thumbnail: a
// chain of small reads through the index at the end of the file, then a frame.
func quickLookReads(size int64) [][2]int64 {
	tail := size - 400<<10
	return [][2]int64{{tail, tail + 4095}, {tail + 99500, tail + 103595}, {tail + 105867, tail + 105898},
		{tail, tail + 4095}, {tail + 828, tail + 59947}, {tail + 59948, tail + 64043}, {tail + 64004, tail + 93679},
		{tail + 93680, tail + 97775}, {tail + 97696, tail + 103499}, {20893428, 21225101}}
}

func (d *revDrive) requests() int {
	d.mu.Lock()
	defer d.mu.Unlock()
	return len(d.ranges)
}

// A thumbnail's chain of small reads costs one Drive round trip per block,
// not one per read; making it again costs none.
func TestThumbnailReadsShareBlocks(t *testing.T) {
	data := make([]byte, 64<<20)
	for i := range data {
		data[i] = byte(i / 7)
	}
	d, _, dav := newRevDrive(t, "clip.mov", data)
	read := func() {
		for _, r := range quickLookReads(int64(len(data))) {
			code, body := davDo(t, dav, "GET", "clip.mov", "Range", fmt.Sprintf("bytes=%d-%d", r[0], r[1]))
			if code != http.StatusPartialContent || !bytes.Equal(body, data[r[0]:r[1]+1]) {
				t.Fatalf("GET %v: %d, %d bytes, equal=%v", r, code, len(body), bytes.Equal(body, data[r[0]:r[1]+1]))
			}
		}
	}
	read()
	if n := d.requests(); n > 3 {
		t.Fatalf("10 thumbnail reads made %d Drive requests: %v", n, d.ranges)
	}
	before := d.requests()
	read()
	if n := d.requests() - before; n != 0 {
		t.Fatalf("making the thumbnail again made %d Drive requests", n)
	}
}

// A changed file (new size or time in the listing) is never served from the
// blocks cached for the old one.
func TestChangedFileIsNotServedFromCache(t *testing.T) {
	d, fs, dav := newRevDrive(t, "clip.mov", bytes.Repeat([]byte("A"), 8<<20))
	fs.listTTL = 0
	if _, body := davDo(t, dav, "GET", "clip.mov", "Range", "bytes=100-199"); string(body) != strings.Repeat("A", 100) {
		t.Fatalf("first read: %q", body)
	}
	d.mu.Lock()
	d.data, d.etag, d.mtime = bytes.Repeat([]byte("B"), 8<<20), `"v2"`, 2
	d.mu.Unlock()
	if _, body := davDo(t, dav, "GET", "clip.mov", "Range", "bytes=100-199"); string(body) != strings.Repeat("B", 100) {
		t.Fatalf("read after the file changed: %q", body[:10])
	}
}

// Opening a video starts fetching its end, where Quick Look looks first.
func TestOpeningAVideoPrefetchesItsIndex(t *testing.T) {
	data := make([]byte, 64<<20)
	d, _, dav := newRevDrive(t, "clip.mov", data)
	req, _ := http.NewRequest("GET", dav.URL+"/V/S/clip.mov", nil)
	req.SetBasicAuth(User, "pw")
	res, err := dav.Client().Do(req)
	if err != nil {
		t.Fatal(err)
	}
	io.ReadFull(res.Body, make([]byte, 64<<10))
	res.Body.Close()
	time.Sleep(200 * time.Millisecond)
	before := d.requests()
	tail := int64(len(data)) - 300<<10
	if code, _ := davDo(t, dav, "GET", "clip.mov", "Range", fmt.Sprintf("bytes=%d-%d", tail, tail+4095)); code != http.StatusPartialContent {
		t.Fatalf("tail read: %d", code)
	}
	if n := d.requests() - before; n != 0 {
		t.Fatalf("the index wasn't prefetched: %d more Drive requests", n)
	}
}
