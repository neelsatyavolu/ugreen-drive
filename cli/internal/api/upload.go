package api

import (
	"context"
	"errors"
	"fmt"
	"io"
	"mime/multipart"
	"net/http"
	"net/url"
	"os"
	"strconv"
	"sync"
)

const (
	// ChunkThreshold matches the web app: larger files use chunked upload so no
	// single request exceeds the tunnel's body limit.
	ChunkThreshold  = 8 << 20
	chunkSize       = 32 << 20
	chunkStreams    = 4
	chunkMaxRetries = 3
)

// MustNotExist asks the server to refuse the upload if the target exists.
const MustNotExist int64 = -1

// ChunkSession is a server-side chunked upload that can be resumed.
type ChunkSession struct {
	UploadID    string `json:"upload_id"`
	ChunkSize   int64  `json:"chunk_size"`
	TotalChunks int    `json:"total_chunks"`
}

// ResumeStore remembers one file's chunked-upload session across runs.
type ResumeStore interface {
	Load() (ChunkSession, bool)
	Save(ChunkSession) error
	Clear()
}

// UploadOptions controls overwrite behavior, resume and progress.
// ExpectMtimeNS nil = overwrite freely; MustNotExist = only create; otherwise
// the target's mtime_ns must still match.
type UploadOptions struct {
	ExpectMtimeNS *int64
	Resume        ResumeStore       // optional; chunked uploads only
	Progress      func(bytes int64) // optional; called as bytes reach the Drive
	OnResume      func(done, total int64)
}

func (o UploadOptions) apply(form url.Values) {
	if o.ExpectMtimeNS != nil {
		form.Set("expect_mtime_ns", strconv.FormatInt(*o.ExpectMtimeNS, 10))
	}
}

func (o UploadOptions) progress(n int64) {
	if o.Progress != nil && n > 0 {
		o.Progress(n)
	}
}

// UploadFile uploads a local file to dir/name, choosing simple or chunked upload.
func (c *Client) UploadFile(ctx context.Context, dir, name string, file *os.File, opts UploadOptions) (Entry, error) {
	info, err := file.Stat()
	if err != nil {
		return Entry{}, err
	}
	if info.Size() >= ChunkThreshold {
		return c.uploadChunked(ctx, dir, name, file, info.Size(), opts)
	}
	return c.uploadSimple(ctx, dir, name, file, opts)
}

type countingReader struct {
	r    io.Reader
	opts UploadOptions
}

func (c countingReader) Read(p []byte) (int, error) {
	n, err := c.r.Read(p)
	c.opts.progress(int64(n))
	return n, err
}

func (c *Client) uploadSimple(ctx context.Context, dir, name string, body io.Reader, opts UploadOptions) (Entry, error) {
	pr, pw := io.Pipe()
	writer := multipart.NewWriter(pw)
	go func() {
		form := url.Values{"path": {CleanPath(dir)}}
		opts.apply(form)
		for key, values := range form {
			for _, v := range values {
				if err := writer.WriteField(key, v); err != nil {
					pw.CloseWithError(err)
					return
				}
			}
		}
		part, err := writer.CreateFormFile("file", name)
		if err == nil {
			_, err = io.Copy(part, countingReader{body, opts})
		}
		if err == nil {
			err = writer.Close()
		}
		pw.CloseWithError(err)
	}()
	req, err := c.newBulkRequest(ctx, http.MethodPost, "/api/upload", nil, pr)
	if err != nil {
		pr.Close()
		return Entry{}, err
	}
	req.Header.Set("Content-Type", writer.FormDataContentType())
	var entry Entry
	err = c.doJSON(req, &entry)
	pr.Close()
	return entry, err
}

// UploadStatus reports which chunks of a session the Drive already has.
func (c *Client) UploadStatus(ctx context.Context, uploadID string) ([]int, error) {
	var status struct {
		Received []int `json:"received"`
	}
	err := c.getJSON(ctx, "/api/upload/status", url.Values{"upload_id": {uploadID}}, &status)
	return status.Received, err
}

// Fingerprint returns the Drive's content fingerprint for a file of the given
// size, or "" when there is no such file (or the size differs).
func (c *Client) Fingerprint(ctx context.Context, p string, size int64) (string, error) {
	var out struct {
		Fingerprint *string `json:"fingerprint"`
	}
	form := url.Values{"path": {CleanPath(p)}, "size": {strconv.FormatInt(size, 10)}}
	if err := c.postForm(ctx, "/api/upload/fingerprint", form, &out); err != nil {
		return "", err
	}
	if out.Fingerprint == nil {
		return "", nil
	}
	return *out.Fingerprint, nil
}

// definite reports whether err means retrying the same session is pointless
// (as opposed to a network error, a 5xx or an interrupt, which can resume).
func definite(err error) bool {
	var apiErr *Error
	return errors.As(err, &apiErr) && apiErr.Status >= 400 && apiErr.Status < 500
}

func (c *Client) startOrResume(ctx context.Context, dir, name string, size int64, opts UploadOptions) (ChunkSession, map[int]bool, error) {
	if opts.Resume != nil {
		if session, ok := opts.Resume.Load(); ok {
			received, err := c.UploadStatus(ctx, session.UploadID)
			switch {
			case err == nil:
				have := make(map[int]bool, len(received))
				var done int64
				for _, i := range received {
					have[i] = true
					done += min(session.ChunkSize, size-int64(i)*session.ChunkSize)
				}
				if opts.OnResume != nil {
					opts.OnResume(done, size)
				}
				opts.progress(done)
				return session, have, nil
			case definite(err):
				opts.Resume.Clear() // expired or gone: start over
			default:
				return ChunkSession{}, nil, err
			}
		}
	}
	var session ChunkSession
	err := c.postForm(ctx, "/api/upload/init", url.Values{
		"path": {CleanPath(dir)}, "name": {name},
		"size": {strconv.FormatInt(size, 10)}, "chunk_size": {strconv.FormatInt(uploadChunkSize(size), 10)},
	}, &session)
	if err != nil {
		return ChunkSession{}, nil, err
	}
	if opts.Resume != nil {
		if err := opts.Resume.Save(session); err != nil {
			return ChunkSession{}, nil, fmt.Errorf("save resume state: %w", err)
		}
	}
	return session, map[int]bool{}, nil
}

// uploadChunkSize splits a file into at least chunkStreams chunks (whole
// MiB, at most chunkSize) so mid-size files use every stream too.
func uploadChunkSize(size int64) int64 {
	const mib = 1 << 20
	per := (size + chunkStreams - 1) / chunkStreams
	return min(chunkSize, (per+mib-1)/mib*mib)
}

func (c *Client) uploadChunked(ctx context.Context, dir, name string, file io.ReaderAt, size int64, opts UploadOptions) (Entry, error) {
	session, have, err := c.startOrResume(ctx, dir, name, size, opts)
	if err != nil {
		return Entry{}, err
	}
	if err := c.sendChunks(ctx, session, file, size, have, opts); err != nil {
		c.finishFailed(session, err, opts)
		return Entry{}, err
	}
	form := url.Values{"upload_id": {session.UploadID}}
	opts.apply(form)
	var entry Entry
	if err := c.postForm(ctx, "/api/upload/complete", form, &entry); err != nil {
		c.finishFailed(session, err, opts)
		return Entry{}, err
	}
	if opts.Resume != nil {
		opts.Resume.Clear()
	}
	return entry, nil
}

// finishFailed aborts the session only when resuming can't help. Without a
// resume store nobody could resume it, so it is always aborted.
func (c *Client) finishFailed(session ChunkSession, err error, opts UploadOptions) {
	if opts.Resume != nil && !definite(err) {
		return
	}
	c.abortUpload(session.UploadID)
	if opts.Resume != nil {
		opts.Resume.Clear()
	}
}

func (c *Client) sendChunks(ctx context.Context, session ChunkSession, file io.ReaderAt, size int64, have map[int]bool, opts UploadOptions) error {
	ctx, cancel := context.WithCancel(ctx)
	defer cancel()
	indices := make(chan int)
	errs := make(chan error, chunkStreams)
	var wg sync.WaitGroup
	for w := 0; w < chunkStreams; w++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for index := range indices {
				if err := c.sendChunkWithRetry(ctx, session, file, size, index, opts); err != nil {
					errs <- err
					cancel()
					return
				}
			}
		}()
	}
feed:
	for index := 0; index < session.TotalChunks; index++ {
		if have[index] {
			continue
		}
		select {
		case indices <- index:
		case <-ctx.Done():
			break feed
		}
	}
	close(indices)
	wg.Wait()
	close(errs)
	if err := <-errs; err != nil {
		return err
	}
	return ctx.Err()
}

func (c *Client) sendChunkWithRetry(ctx context.Context, session ChunkSession, file io.ReaderAt, size int64, index int, opts UploadOptions) error {
	offset := int64(index) * session.ChunkSize
	length := min(session.ChunkSize, size-offset)
	var err error
	for attempt := 0; attempt < chunkMaxRetries; attempt++ {
		err = c.sendChunk(ctx, session.UploadID, index, io.NewSectionReader(file, offset, length), length)
		if err == nil {
			opts.progress(length)
			return nil
		}
		if ctx.Err() != nil || definite(err) {
			return err
		}
	}
	return fmt.Errorf("upload piece %d failed after %d tries: %w", index, chunkMaxRetries, err)
}

func (c *Client) sendChunk(ctx context.Context, uploadID string, index int, body io.Reader, length int64) error {
	q := url.Values{"upload_id": {uploadID}, "index": {strconv.Itoa(index)}}
	req, err := c.newBulkRequest(ctx, http.MethodPut, "/api/upload/chunk", q, body)
	if err != nil {
		return err
	}
	req.ContentLength = length
	req.Header.Set("Content-Type", "application/octet-stream")
	return c.doJSON(req, nil)
}

func (c *Client) abortUpload(uploadID string) {
	// Best effort: the server also expires abandoned sessions on its own.
	_ = c.postForm(context.Background(), "/api/upload/abort", url.Values{"upload_id": {uploadID}}, nil)
}
