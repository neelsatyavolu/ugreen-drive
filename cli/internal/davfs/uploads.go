package davfs

import (
	"context"
	"os"
	"sync"
	"time"

	"github.com/neelsatyavolu/ugreen-drive/cli/internal/api"
)

// progressEvery limits how often an active upload is reported.
const progressEvery = 500 * time.Millisecond

// Upload is one file being sent to the Drive, reported through FS.OnUpload
// (the Mac app shows these as transfers).
type Upload struct {
	ID    int64  `json:"id"`
	Path  string `json:"path"` // share folder name + path inside the share
	Size  int64  `json:"size"`
	Sent  int64  `json:"sent"`
	State string `json:"state"` // "active", "done" or "failed"
	Error string `json:"error,omitempty"`
}

// upload sends a local file to t on the Drive (replacing what is there
// unless opts say otherwise) and reports its progress.
func (f *FS) upload(ctx context.Context, t target, localPath string, opts api.UploadOptions) error {
	file, err := os.Open(localPath)
	if err != nil {
		return err
	}
	defer file.Close()
	info, err := file.Stat()
	if err != nil {
		return err
	}
	report := f.reporter(t, info.Size())
	opts.Progress = report.progress
	dir, base := api.SplitPath(t.rel)
	entry, err := f.clientFor(t.share).UploadFile(ctx, dir, base, file, opts)
	if err != nil {
		f.cacheDropParent(t)
		err = f.osErr("write", t.rel, err)
		report.finish(err)
		return err
	}
	f.cachePut(t, entry)
	report.finish(nil)
	return nil
}

type uploadReport struct {
	fs   *FS
	mu   sync.Mutex
	u    Upload
	last time.Time
}

// reporter starts reporting an upload. Empty files (Finder's LOCK
// placeholders) aren't worth showing, so they stay silent.
func (f *FS) reporter(t target, size int64) *uploadReport {
	r := &uploadReport{fs: f, u: Upload{Path: shareName(t.share) + "/" + t.rel, Size: size, State: "active"}}
	if size > 0 && f.OnUpload != nil {
		f.mu.Lock()
		f.uploadSeq++
		r.u.ID = f.uploadSeq
		f.mu.Unlock()
		r.last = time.Now()
		f.OnUpload(r.u)
	}
	return r
}

func (r *uploadReport) active() bool { return r.u.ID != 0 }

func (r *uploadReport) progress(n int64) {
	if !r.active() {
		return
	}
	r.mu.Lock()
	defer r.mu.Unlock()
	r.u.Sent += n
	if time.Since(r.last) >= progressEvery {
		r.last = time.Now()
		r.fs.OnUpload(r.u)
	}
}

func (r *uploadReport) finish(err error) {
	if !r.active() {
		return
	}
	r.mu.Lock()
	defer r.mu.Unlock()
	if err != nil {
		r.u.State, r.u.Error = "failed", err.Error()
	} else {
		r.u.State, r.u.Sent = "done", r.u.Size
	}
	r.fs.OnUpload(r.u)
}
