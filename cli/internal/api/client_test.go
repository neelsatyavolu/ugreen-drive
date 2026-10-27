package api

import (
	"context"
	"io"
	"net/http"
	"net/http/httptest"
	"net/url"
	"testing"
)

func TestDownloadFromSkipsAheadWhenServerIgnoresRange(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		io.WriteString(w, "0123456789") // always 200, whole file
	}))
	defer srv.Close()
	base, _ := url.Parse(srv.URL)
	c := &Client{Base: base, HTTP: srv.Client()}
	body, err := c.DownloadFrom(context.Background(), "a.txt", 7)
	if err != nil {
		t.Fatal(err)
	}
	defer body.Close()
	got, _ := io.ReadAll(body)
	if string(got) != "789" {
		t.Fatalf("got %q, want 789", got)
	}
}
