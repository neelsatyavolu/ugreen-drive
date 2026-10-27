package davfs

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"net/url"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/neelsatyavolu/ugreen-drive/cli/internal/api"
)

// slowDrive answers /api/me and /api/files, as slowly as asked.
type slowDrive struct {
	meDelay, listDelay atomic.Int64 // nanoseconds
	meCalls, listCalls atomic.Int32
	mu                 sync.Mutex
	items              []api.Entry // the folder "Documents" in share S
}

func newSlowDrive(t *testing.T) (*slowDrive, *FS) {
	t.Helper()
	d := &slowDrive{items: []api.Entry{{Name: "a.mov", Path: "Documents/a.mov", Size: 1}}}
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/api/me":
			d.meCalls.Add(1)
			time.Sleep(time.Duration(d.meDelay.Load()))
			json.NewEncoder(w).Encode(map[string]any{"authenticated": true,
				"shares": []map[string]any{{"id": "S", "name": "S", "can_read": true, "can_write": true}}})
		case "/api/files":
			d.listCalls.Add(1)
			d.mu.Lock()
			items := append([]api.Entry(nil), d.items...)
			d.mu.Unlock()
			time.Sleep(time.Duration(d.listDelay.Load()))
			if r.URL.Query().Get("path") == "" {
				items = []api.Entry{{Name: "Documents", Path: "Documents", IsDir: true}}
			}
			json.NewEncoder(w).Encode(map[string]any{"path": r.URL.Query().Get("path"), "items": items})
		default:
			http.NotFound(w, r)
		}
	}))
	t.Cleanup(srv.Close)
	base, _ := url.Parse(srv.URL)
	fs := New(&api.Client{Base: base, Token: "ugd_test", HTTP: srv.Client()}, t.TempDir())
	return d, fs
}

func timed(t *testing.T, fn func() error) time.Duration {
	t.Helper()
	start := time.Now()
	if err := fn(); err != nil {
		t.Fatal(err)
	}
	return time.Since(start)
}

func eventually(t *testing.T, what string, cond func() bool) {
	t.Helper()
	for deadline := time.Now().Add(3 * time.Second); time.Now().Before(deadline); time.Sleep(5 * time.Millisecond) {
		if cond() {
			return
		}
	}
	t.Fatalf("timed out waiting for %s", what)
}

// Refreshing the share list (UGOS can take seconds) must never hold up Finder.
func TestStaleShareListIsServedWhileRefreshing(t *testing.T) {
	d, fs := newSlowDrive(t)
	fs.sharesTTL = 10 * time.Millisecond
	ctx := context.Background()
	if _, err := fs.Stat(ctx, "/S"); err != nil {
		t.Fatal(err)
	}
	d.meDelay.Store(int64(time.Second))
	time.Sleep(20 * time.Millisecond)
	for i := 0; i < 3; i++ {
		if took := timed(t, func() error { _, err := fs.Stat(ctx, "/S"); return err }); took > 200*time.Millisecond {
			t.Fatalf("Stat waited %v for the share list", took)
		}
	}
	eventually(t, "one background refresh", func() bool { return d.meCalls.Load() == 2 })
	time.Sleep(50 * time.Millisecond)
	if n := d.meCalls.Load(); n != 2 {
		t.Fatalf("%d /api/me calls, want one refresh at a time", n)
	}
}

// The startup /api/me answer fills the share list, so mounting doesn't ask twice.
func TestSetSharesSeedsTheList(t *testing.T) {
	d, fs := newSlowDrive(t)
	fs.SetShares([]api.Share{{ID: "S", Name: "S", CanWrite: true}})
	if _, err := fs.Stat(context.Background(), "/S"); err != nil {
		t.Fatal(err)
	}
	if n := d.meCalls.Load(); n != 0 {
		t.Fatalf("%d /api/me calls after seeding", n)
	}
}

// Finder showing a folder whose listing is past its TTL gets it at once while
// one background fetch refreshes it; parallel views share that fetch. Looking
// up a name waits for the fresh listing (sizes must be current).
func TestStaleListingIsServedWhileRefreshing(t *testing.T) {
	d, fs := newSlowDrive(t)
	fs.listTTL = 10 * time.Millisecond
	ctx := context.Background()
	docs, err := fs.resolve(ctx, "/S/Documents")
	if err != nil {
		t.Fatal(err)
	}
	if _, err := fs.readDir(ctx, node{t: docs}); err != nil {
		t.Fatal(err)
	}
	calls := d.listCalls.Load()
	d.listDelay.Store(int64(300 * time.Millisecond))
	d.mu.Lock()
	d.items = append(d.items, api.Entry{Name: "b.mov", Path: "Documents/b.mov", Size: 2})
	d.mu.Unlock()
	time.Sleep(20 * time.Millisecond)
	var wg sync.WaitGroup
	for i := 0; i < 5; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			if took := timed(t, func() error { _, err := fs.readDir(ctx, node{t: docs}); return err }); took > 150*time.Millisecond {
				t.Errorf("listing the folder waited %v for a stale listing", took)
			}
		}()
	}
	wg.Wait()
	eventually(t, "the background fetch", func() bool { return d.listCalls.Load() > calls })
	time.Sleep(50 * time.Millisecond) // still within its 300 ms
	if n := d.listCalls.Load() - calls; n != 1 {
		t.Fatalf("%d fetches for one stale folder, want 1", n)
	}
	if _, err := fs.Stat(ctx, "/S/Documents/b.mov"); err != nil {
		t.Fatalf("lookup didn't wait for the fresh listing: %v", err)
	}
}

// A listing fetched before a local change (an upload landing) must not
// overwrite the cache that already shows the change.
func TestRefreshNeverUndoesALocalChange(t *testing.T) {
	d, fs := newSlowDrive(t)
	fs.listTTL = 10 * time.Millisecond
	ctx := context.Background()
	if _, err := fs.Stat(ctx, "/S/Documents/a.mov"); err != nil {
		t.Fatal(err)
	}
	d.listDelay.Store(int64(200 * time.Millisecond))
	time.Sleep(20 * time.Millisecond)
	fs.Stat(ctx, "/S/Documents/a.mov") // stale: starts a refresh that won't see c.mov
	docs := target{share: api.Share{ID: "S", Name: "S"}, rel: "Documents/c.mov"}
	fs.cachePut(docs, api.Entry{Name: "c.mov", Path: "Documents/c.mov", Size: 3})
	time.Sleep(300 * time.Millisecond) // the refresh has finished
	fs.listTTL = time.Hour
	if _, err := fs.Stat(ctx, "/S/Documents/c.mov"); err != nil {
		t.Fatalf("the uploaded file vanished from the listing: %v", err)
	}
}
