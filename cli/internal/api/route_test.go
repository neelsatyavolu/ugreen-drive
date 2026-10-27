package api

import (
	"context"
	"io"
	"net/http"
	"net/http/httptest"
	"net/url"
	"strings"
	"sync"
	"testing"
	"time"
)

// Finder abandons reads all the time; a request its caller cancelled says
// nothing about the LAN, so the route must stay.
func TestCancelledRequestKeepsLANRoute(t *testing.T) {
	started := make(chan struct{})
	lan := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		close(started)
		<-r.Context().Done()
	}))
	defer lan.Close()
	internet := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		t.Error("a cancelled request was retried over the internet")
	}))
	defer internet.Close()
	base, _ := url.Parse(internet.URL)
	lanBase, _ := url.Parse(lan.URL)
	route := &LANRoute{}
	route.Set(&Route{Base: lanBase, Token: "ugl_x", Expires: time.Now().Add(time.Hour)})
	c := &Client{Base: base, Token: "ugd_x", LAN: route}
	ctx, cancel := context.WithCancel(context.Background())
	go func() { <-started; cancel() }()
	if _, _, err := c.DownloadRange(ctx, "a.bin", 0, 10); err == nil {
		t.Fatal("cancelled request succeeded")
	}
	if route.Get() == nil {
		t.Fatal("cancelling a request dropped the LAN route")
	}
}

type countingTransport struct {
	mu    sync.Mutex
	paths []string
}

func (c *countingTransport) RoundTrip(req *http.Request) (*http.Response, error) {
	c.mu.Lock()
	c.paths = append(c.paths, req.URL.Path)
	c.mu.Unlock()
	return http.DefaultTransport.RoundTrip(req)
}

// Large transfers go over their own connections (Bulk); everything else
// keeps the warm shared one.
func TestBulkTransfersUseTheBulkClient(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/api/files":
			w.Write([]byte(`{"path":"","items":[]}`))
		case "/api/download", "/api/speedtest/download":
			http.ServeContent(w, r, "", time.Time{}, strings.NewReader("0123456789"))
		default:
			io.Copy(io.Discard, r.Body)
			w.Write([]byte(`{}`))
		}
	}))
	defer srv.Close()
	base, _ := url.Parse(srv.URL)
	shared, bulk := &countingTransport{}, &countingTransport{}
	c := &Client{Base: base, Token: "ugd_x", HTTP: &http.Client{Transport: shared}, Bulk: &http.Client{Transport: bulk}}
	ctx := context.Background()
	if _, err := c.List(ctx, ""); err != nil {
		t.Fatal(err)
	}
	if _, _, err := c.DownloadRange(ctx, "a.bin", 2, 4); err != nil {
		t.Fatal(err)
	}
	body, _, err := c.DownloadVersionFrom(ctx, "a.bin", 0)
	if err != nil {
		t.Fatal(err)
	}
	body.Close()
	if err := c.SpeedTestUpload(ctx, strings.NewReader("abc"), 3); err != nil {
		t.Fatal(err)
	}
	if _, _, err := c.SpeedTestRange(ctx, 10, 0, 5); err != nil {
		t.Fatal(err)
	}
	if strings.Join(shared.paths, " ") != "/api/files" {
		t.Fatalf("shared connection carried %v", shared.paths)
	}
	if len(bulk.paths) != 4 {
		t.Fatalf("bulk connections carried %v", bulk.paths)
	}
}

// The first bytes of a read are latency-bound: Warm keeps them on the warm
// shared connection instead of a bulk one that may need a TLS handshake.
func TestWarmReadsUseTheSharedClient(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		http.ServeContent(w, r, "", time.Time{}, strings.NewReader("0123456789"))
	}))
	defer srv.Close()
	base, _ := url.Parse(srv.URL)
	shared, bulk := &countingTransport{}, &countingTransport{}
	c := &Client{Base: base, Token: "ugd_x", HTTP: &http.Client{Transport: shared}, Bulk: &http.Client{Transport: bulk}}
	if _, _, err := c.DownloadRange(Warm(context.Background()), "a.bin", 0, 4); err != nil {
		t.Fatal(err)
	}
	if len(shared.paths) != 1 || len(bulk.paths) != 0 {
		t.Fatalf("shared %v, bulk %v", shared.paths, bulk.paths)
	}
}
