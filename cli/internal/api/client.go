// Package api is a small client for the Ugreen Drive HTTP API.
package api

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"path"
	"strconv"
	"strings"
)

// Error is a non-2xx response from the Drive.
type Error struct {
	Status int
	Detail string
}

func (e *Error) Error() string {
	if e.Detail != "" {
		return e.Detail
	}
	return fmt.Sprintf("the Drive returned HTTP %d", e.Status)
}

// Client talks to one Drive server as one signed-in user.
type Client struct {
	Base  *url.URL
	Token string
	Share string
	HTTP  *http.Client
	// Bulk carries large transfers (downloads, upload bodies) when set; see
	// newBulkRequest. Default: HTTP.
	Bulk      *http.Client
	UserAgent string
	LAN       *LANRoute // optional: use the Drive's LAN address when set
}

type bulkKey struct{}

// newBulkRequest is newRequest for a large transfer, which goes over Bulk.
func (c *Client) newBulkRequest(ctx context.Context, method, endpoint string, query url.Values, body io.Reader) (*http.Request, error) {
	return c.newRequest(context.WithValue(ctx, bulkKey{}, true), method, endpoint, query, body)
}

type warmKey struct{}

// Warm marks ctx's requests as latency-bound (a small read, the first bytes
// of a large one): they stay on the warm shared connection, since a bulk one
// may first need a TLS handshake.
func Warm(ctx context.Context) context.Context { return context.WithValue(ctx, warmKey{}, true) }

// clientFor picks the connections req goes over.
func (c *Client) clientFor(req *http.Request) *http.Client {
	ctx := req.Context()
	if c.Bulk != nil && ctx.Value(bulkKey{}) != nil && ctx.Value(warmKey{}) == nil {
		return c.Bulk
	}
	return c.httpClient()
}

// Entry is one file or folder, as returned by /api/files and friends.
type Entry struct {
	Name    string `json:"name"`
	Path    string `json:"path"`
	IsDir   bool   `json:"is_dir"`
	Size    int64  `json:"size"`
	Mtime   string `json:"mtime"`
	MtimeNS int64  `json:"mtime_ns"`
}

// Listing is a folder listing.
type Listing struct {
	Path  string  `json:"path"`
	Share string  `json:"share"`
	Items []Entry `json:"items"`
}

// Share is one share the account can open. ID is what X-Drive-Share takes
// (personal folders look like "~username"); Name is for display.
type Share struct {
	ID       string `json:"id"`
	Name     string `json:"name"`
	Kind     string `json:"kind"`
	CanWrite bool   `json:"can_write"`
	// Encrypted personal folders (see UnlockPersonal).
	Encrypted        bool     `json:"encrypted,omitempty"`
	Locked           bool     `json:"locked,omitempty"`
	NeedsOwnerSignIn bool     `json:"needs_owner_signin,omitempty"`
	ExpiresAt        *float64 `json:"expires_at,omitempty"`
}

// Me is the signed-in account (/api/me).
type Me struct {
	Authenticated bool    `json:"authenticated"`
	Email         string  `json:"email"`
	Username      string  `json:"nas_username"`
	IsAdmin       bool    `json:"is_admin"`
	Share         string  `json:"share"`
	Shares        []Share `json:"shares"`
	LANOrigin     string  `json:"lan_origin,omitempty"` // the Drive's LAN address
}

// FindShare matches a share by id, then by display name.
func (m Me) FindShare(idOrName string) (Share, bool) {
	for _, s := range m.Shares {
		if s.ID == idOrName {
			return s, true
		}
	}
	for _, s := range m.Shares {
		if s.Name == idOrName {
			return s, true
		}
	}
	return Share{}, false
}

// SearchResult is /api/search output.
type SearchResult struct {
	Query     string  `json:"query"`
	Results   []Entry `json:"results"`
	Truncated bool    `json:"truncated"`
	HasMore   bool    `json:"has_more"`
}

// DeleteResult says whether an item went to the recycle bin or was removed.
type DeleteResult struct {
	Action string `json:"action"`
	Path   string `json:"path"`
}

// CleanPath turns user input like "/Documents//Ep1/" into "Documents/Ep1".
func CleanPath(p string) string {
	p = strings.ReplaceAll(strings.TrimSpace(p), "\\", "/")
	cleaned := path.Clean("/" + p)
	return strings.TrimPrefix(cleaned, "/")
}

// SplitPath returns the parent folder and final name of a remote path.
func SplitPath(p string) (dir, name string) {
	p = CleanPath(p)
	dir, name = path.Split(p)
	return strings.TrimSuffix(dir, "/"), name
}

func (c *Client) httpClient() *http.Client {
	if c.HTTP != nil {
		return c.HTTP
	}
	return http.DefaultClient
}

func (c *Client) newRequest(ctx context.Context, method, endpoint string, query url.Values, body io.Reader) (*http.Request, error) {
	base, token := c.Base, c.Token
	if r := c.LAN.Get(); r != nil {
		base, token = r.Base, r.Token
	}
	u := *base
	u.Path = endpoint
	if query != nil {
		u.RawQuery = query.Encode()
	}
	req, err := http.NewRequestWithContext(ctx, method, u.String(), body)
	if err != nil {
		return nil, err
	}
	if token != "" {
		req.Header.Set("Authorization", "Bearer "+token)
	}
	if c.Share != "" {
		req.Header.Set("X-Drive-Share", c.Share)
	}
	if c.UserAgent != "" {
		req.Header.Set("User-Agent", c.UserAgent)
	}
	return req, nil
}

// do sends req and turns non-2xx responses into *Error.
func (c *Client) do(req *http.Request) (*http.Response, error) {
	res, err := c.clientFor(req).Do(req)
	// The caller gave up: says nothing about the LAN (a timeout does).
	cancelled := err != nil && errors.Is(req.Context().Err(), context.Canceled)
	if c.LAN.carried(req) && !cancelled && (err != nil || res.StatusCode == http.StatusUnauthorized) {
		// Left the home network, or the LAN token lapsed: back to the
		// internet. A LAN 401 is never "signed out".
		if res != nil {
			res.Body.Close()
		}
		c.LAN.Drop()
		if retry := c.retryOverInternet(req); retry != nil {
			return c.do(retry)
		}
		return nil, &Error{Status: http.StatusServiceUnavailable, Detail: "the network changed during this request; try again"}
	}
	if err != nil {
		return nil, fmt.Errorf("can't reach %s: %w", c.Base.Host, err)
	}
	if res.StatusCode >= 200 && res.StatusCode < 300 {
		return res, nil
	}
	defer res.Body.Close()
	var body struct {
		Detail any `json:"detail"`
	}
	raw, _ := io.ReadAll(io.LimitReader(res.Body, 64<<10))
	apiErr := &Error{Status: res.StatusCode}
	if json.Unmarshal(raw, &body) == nil && body.Detail != nil {
		if s, ok := body.Detail.(string); ok {
			apiErr.Detail = s
		} else {
			encoded, _ := json.Marshal(body.Detail)
			apiErr.Detail = string(encoded)
		}
	}
	return nil, apiErr
}

func (c *Client) doJSON(req *http.Request, out any) error {
	res, err := c.do(req)
	if err != nil {
		return err
	}
	defer res.Body.Close()
	if out == nil {
		_, err = io.Copy(io.Discard, res.Body)
		return err
	}
	if err := json.NewDecoder(res.Body).Decode(out); err != nil {
		return fmt.Errorf("unexpected response from the Drive: %w", err)
	}
	return nil
}

func (c *Client) getJSON(ctx context.Context, endpoint string, query url.Values, out any) error {
	req, err := c.newRequest(ctx, http.MethodGet, endpoint, query, nil)
	if err != nil {
		return err
	}
	return c.doJSON(req, out)
}

func (c *Client) postForm(ctx context.Context, endpoint string, form url.Values, out any) error {
	req, err := c.newRequest(ctx, http.MethodPost, endpoint, nil, strings.NewReader(form.Encode()))
	if err != nil {
		return err
	}
	req.Header.Set("Content-Type", "application/x-www-form-urlencoded")
	return c.doJSON(req, out)
}

// Me returns the account behind the token.
func (c *Client) Me(ctx context.Context) (Me, error) {
	var me Me
	err := c.getJSON(ctx, "/api/me", nil, &me)
	return me, err
}

// List returns the contents of a folder.
func (c *Client) List(ctx context.Context, dir string) (Listing, error) {
	var listing Listing
	err := c.getJSON(ctx, "/api/files", url.Values{"path": {CleanPath(dir)}}, &listing)
	return listing, err
}

// Stat finds one entry by listing its parent. The share root is a folder.
func (c *Client) Stat(ctx context.Context, p string) (Entry, bool, error) {
	p = CleanPath(p)
	if p == "" {
		return Entry{Name: "", Path: "", IsDir: true}, true, nil
	}
	dir, name := SplitPath(p)
	listing, err := c.List(ctx, dir)
	if err != nil {
		return Entry{}, false, err
	}
	for _, item := range listing.Items {
		if item.Name == name {
			return item, true, nil
		}
	}
	return Entry{}, false, nil
}

// Search runs the Drive's recursive name search.
func (c *Client) Search(ctx context.Context, query, under string, limit int) (SearchResult, error) {
	var result SearchResult
	q := url.Values{"q": {query}, "path": {CleanPath(under)}}
	if limit > 0 {
		q.Set("limit", fmt.Sprint(limit))
	}
	err := c.getJSON(ctx, "/api/search", q, &result)
	return result, err
}

// Download streams a file's bytes. The caller closes the reader.
func (c *Client) Download(ctx context.Context, p string) (io.ReadCloser, error) {
	return c.DownloadFrom(ctx, p, 0)
}

// DownloadFrom streams a file's bytes starting at offset (a Range request).
// If the server ignores the range, the skipped bytes are read and discarded.
func (c *Client) DownloadFrom(ctx context.Context, p string, offset int64) (io.ReadCloser, error) {
	body, _, err := c.DownloadVersionFrom(ctx, p, offset)
	return body, err
}

// DownloadVersionFrom is DownloadFrom plus the file's version (see Version).
func (c *Client) DownloadVersionFrom(ctx context.Context, p string, offset int64) (io.ReadCloser, string, error) {
	return c.streamFrom(ctx, "/api/download", url.Values{"path": {CleanPath(p)}, "inline": {"0"}}, offset)
}

func (c *Client) streamFrom(ctx context.Context, endpoint string, query url.Values, offset int64) (io.ReadCloser, string, error) {
	req, err := c.newBulkRequest(ctx, http.MethodGet, endpoint, query, nil)
	if err != nil {
		return nil, "", err
	}
	if offset > 0 {
		req.Header.Set("Range", fmt.Sprintf("bytes=%d-", offset))
	}
	res, err := c.do(req)
	if err != nil {
		return nil, "", err
	}
	if offset > 0 && res.StatusCode != http.StatusPartialContent {
		if _, err := io.CopyN(io.Discard, res.Body, offset); err != nil {
			res.Body.Close()
			return nil, "", fmt.Errorf("skip to byte %d: %w", offset, err)
		}
	}
	return res.Body, Version(res), nil
}

// Version identifies which version of a file a download response is from
// (ETag, Last-Modified and total size), so reads assembled from several
// requests can tell if the file was replaced in between.
func Version(res *http.Response) string {
	total := res.Header.Get("Content-Length")
	if cr := res.Header.Get("Content-Range"); cr != "" {
		if i := strings.LastIndexByte(cr, '/'); i >= 0 {
			total = cr[i+1:]
		}
	}
	return res.Header.Get("ETag") + "|" + res.Header.Get("Last-Modified") + "|" + total
}

// ErrNoRange means the server answered a range request with the whole file.
var ErrNoRange = errors.New("the Drive doesn't support byte ranges")

// DownloadRange returns length bytes of a file starting at offset (one
// chunk of a parallel read) and the file's Version.
func (c *Client) DownloadRange(ctx context.Context, p string, offset, length int64) ([]byte, string, error) {
	return c.chunk(ctx, "/api/download", url.Values{"path": {CleanPath(p)}, "inline": {"0"}}, offset, length)
}

// SpeedTestFrom streams the Drive's synthetic speed-test file (size bytes)
// from offset — the same request shape as a real download.
func (c *Client) SpeedTestFrom(ctx context.Context, size, offset int64) (io.ReadCloser, string, error) {
	return c.streamFrom(ctx, "/api/speedtest/download", url.Values{"size": {strconv.FormatInt(size, 10)}}, offset)
}

// SpeedTestRange is DownloadRange for the speed-test file.
func (c *Client) SpeedTestRange(ctx context.Context, size, offset, length int64) ([]byte, string, error) {
	return c.chunk(ctx, "/api/speedtest/download", url.Values{"size": {strconv.FormatInt(size, 10)}}, offset, length)
}

// SpeedTestUpload sends one piece (at most 32 MiB) that the Drive discards.
func (c *Client) SpeedTestUpload(ctx context.Context, body io.Reader, length int64) error {
	req, err := c.newBulkRequest(ctx, http.MethodPost, "/api/speedtest/upload", nil, body)
	if err != nil {
		return err
	}
	req.ContentLength = length
	req.Header.Set("Content-Type", "application/octet-stream")
	return c.doJSON(req, nil)
}

func (c *Client) chunk(ctx context.Context, endpoint string, query url.Values, offset, length int64) ([]byte, string, error) {
	req, err := c.newBulkRequest(ctx, http.MethodGet, endpoint, query, nil)
	if err != nil {
		return nil, "", err
	}
	req.Header.Set("Range", fmt.Sprintf("bytes=%d-%d", offset, offset+length-1))
	res, err := c.do(req)
	if err != nil {
		return nil, "", err
	}
	defer res.Body.Close()
	if res.StatusCode != http.StatusPartialContent {
		return nil, "", ErrNoRange
	}
	buf := make([]byte, length)
	if _, err := io.ReadFull(res.Body, buf); err != nil {
		return nil, "", fmt.Errorf("read bytes %d–%d: %w", offset, offset+length, err)
	}
	return buf, Version(res), nil
}

// DownloadZip streams a zip of the given files/folders.
func (c *Client) DownloadZip(ctx context.Context, paths ...string) (io.ReadCloser, error) {
	q := url.Values{}
	for _, p := range paths {
		q.Add("path", CleanPath(p))
	}
	req, err := c.newBulkRequest(ctx, http.MethodGet, "/api/download/zip", q, nil)
	if err != nil {
		return nil, err
	}
	res, err := c.do(req)
	if err != nil {
		return nil, err
	}
	return res.Body, nil
}

// Mkdir creates one folder named name inside parent.
func (c *Client) Mkdir(ctx context.Context, parent, name string) (Entry, error) {
	var entry Entry
	err := c.postForm(ctx, "/api/mkdir", url.Values{"path": {CleanPath(parent)}, "name": {name}}, &entry)
	return entry, err
}

// Rename renames an item in place.
func (c *Client) Rename(ctx context.Context, p, newName string) error {
	return c.postForm(ctx, "/api/rename", url.Values{"path": {CleanPath(p)}, "new_name": {newName}}, nil)
}

// Move moves an item into destDir.
func (c *Client) Move(ctx context.Context, p, destDir string) error {
	return c.postForm(ctx, "/api/move", url.Values{"path": {CleanPath(p)}, "dest": {CleanPath(destDir)}}, nil)
}

// Delete moves an item to the share's recycle bin (or deletes it when there is none).
func (c *Client) Delete(ctx context.Context, p string) (DeleteResult, error) {
	var result DeleteResult
	err := c.postForm(ctx, "/api/delete", url.Values{"path": {CleanPath(p)}}, &result)
	return result, err
}

// Logout revokes the token on the server.
func (c *Client) Logout(ctx context.Context) error {
	req, err := c.newRequest(ctx, http.MethodPost, "/api/cli/logout", nil, nil)
	if err != nil {
		return err
	}
	return c.doJSON(req, nil)
}
