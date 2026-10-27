package davfs

import (
	"context"
	"net/http"
	"net/url"
	"strconv"
	"strings"
	"sync/atomic"

	"github.com/neelsatyavolu/ugreen-drive/cli/internal/api"
)

// request is what the file system needs to know about the WebDAV request
// it is serving; x/net/webdav only hands it a context.
type request struct {
	method  string
	length  int64  // PUT body length; -1 if unknown
	readEnd int64  // GET of one bounded range: the offset after its last byte; else -1
	moveSrc string // MOVE source and destination, as cleaned names
	moveDst string
	broken  atomic.Bool // a read or write in this request failed
}

type requestKey struct{}

func withRequest(r *http.Request, prefix string) *http.Request {
	req := &request{method: r.Method, length: -1, readEnd: -1}
	switch r.Method {
	case http.MethodGet:
		req.readEnd = rangeEnd(r.Header.Get("Range"))
	case http.MethodPut:
		req.length = r.ContentLength
	case "MOVE":
		req.moveSrc = api.CleanPath(strings.TrimPrefix(r.URL.Path, prefix))
		if dst, err := url.Parse(r.Header.Get("Destination")); err == nil {
			req.moveDst = api.CleanPath(strings.TrimPrefix(dst.Path, prefix))
		}
	}
	return r.WithContext(context.WithValue(r.Context(), requestKey{}, req))
}

func requestOf(ctx context.Context) *request {
	if req, ok := ctx.Value(requestKey{}).(*request); ok {
		return req
	}
	return &request{length: -1, readEnd: -1}
}

// rangeEnd returns b+1 for a single "bytes=a-b" range, so reads stop where
// the request does; -1 for anything else (no range, open-ended, several).
func rangeEnd(header string) int64 {
	spec, ok := strings.CutPrefix(strings.TrimSpace(header), "bytes=")
	if !ok || strings.Contains(spec, ",") {
		return -1
	}
	from, to, ok := strings.Cut(spec, "-")
	a, errA := strconv.ParseInt(strings.TrimSpace(from), 10, 64)
	b, errB := strconv.ParseInt(strings.TrimSpace(to), 10, 64)
	if !ok || errA != nil || errB != nil || b < a {
		return -1
	}
	return b + 1
}
