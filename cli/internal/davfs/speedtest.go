package davfs

import (
	"context"
	"errors"
	"io"
	"io/fs"
	"os"
	"path"
	"regexp"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/neelsatyavolu/ugreen-drive/cli/internal/api"
	"golang.org/x/net/webdav"
)

// SpeedTestDir is a hidden folder at the top of the volume that behaves like
// any Drive folder to Finder, but its files are the Drive's synthetic
// speed-test stream, so the Mac app can measure exactly what Finder gets
// (macOS WebDAV client → this helper → tunnel → Drive) without writing to a
// share:
//   - reading "download-<MB>-<anything>.bin" streams that many MB through the
//     same ranged / parallel read path as a real file;
//   - writing any file sends its bytes in the same 32 MiB × 4 chunks as a
//     real upload, to an endpoint that discards them.
const SpeedTestDir = ".Ugreen Drive Speed Test"

const (
	speedChunk   = 32 << 20 // like real chunked uploads
	speedStreams = 4
)

var speedDownloadName = regexp.MustCompile(`^download-(\d{1,4})(-[A-Za-z0-9-]+)?\.bin$`)

// WithSpeedTest serves SpeedTestDir on top of fs.
func WithSpeedTest(fs *FS) webdav.FileSystem {
	return &speedTestFS{FS: fs, written: map[string]fileInfo{}}
}

type speedTestFS struct {
	*FS
	mu      sync.Mutex
	written map[string]fileInfo // files written into the folder (until deleted)
}

// speedName returns the file name inside SpeedTestDir ("" = the folder
// itself), or ok=false for names outside it.
func speedName(name string) (string, bool) {
	clean := api.CleanPath(name)
	if clean == SpeedTestDir {
		return "", true
	}
	rest, ok := strings.CutPrefix(clean, SpeedTestDir+"/")
	if !ok || strings.Contains(rest, "/") {
		return "", false
	}
	return rest, true
}

func (s *speedTestFS) stat(name string) (fileInfo, bool) {
	if name == "" {
		return fileInfo{name: SpeedTestDir, dir: true, mtime: s.start}, true
	}
	if m := speedDownloadName.FindStringSubmatch(name); m != nil {
		mb, _ := strconv.ParseInt(m[1], 10, 64)
		if mb > 0 && mb <= 512 {
			return fileInfo{name: name, size: mb << 20, mtime: s.start}, true
		}
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	info, ok := s.written[name]
	return info, ok
}

func (s *speedTestFS) Stat(ctx context.Context, name string) (os.FileInfo, error) {
	inside, ok := speedName(name)
	if !ok {
		return s.FS.Stat(ctx, name)
	}
	info, ok := s.stat(inside)
	if !ok {
		return nil, pathErr("stat", name, os.ErrNotExist)
	}
	return info, nil
}

func (s *speedTestFS) OpenFile(ctx context.Context, name string, flag int, perm os.FileMode) (webdav.File, error) {
	inside, ok := speedName(name)
	if !ok {
		f, err := s.FS.OpenFile(ctx, name, flag, perm)
		if err == nil && api.CleanPath(name) == "" {
			// List the folder at the top of the volume: macOS treats a folder
			// missing from its parent's listing as deleted.
			info, _ := s.stat("")
			f = &rootDir{File: f, extra: info}
		}
		return f, err
	}
	if flag&(os.O_WRONLY|os.O_RDWR|os.O_CREATE|os.O_TRUNC|os.O_APPEND) != 0 {
		if inside == "" {
			return nil, pathErr("open", name, os.ErrPermission)
		}
		tmp, err := s.local.tempFile()
		if err != nil {
			return nil, err
		}
		return &speedWrite{File: tmp, ctx: ctx, fs: s, name: inside, req: requestOf(ctx)}, nil
	}
	info, ok := s.stat(inside)
	switch {
	case !ok:
		return nil, pathErr("open", name, os.ErrNotExist)
	case info.dir:
		return &speedDir{fs: s, info: info}, nil
	case speedDownloadName.MatchString(inside):
		src := speedSource{client: s.client, size: info.size}
		return &remoteFile{ctx: ctx, fs: s.FS, src: src, rel: name, info: info}, nil
	default: // a file the test wrote: its bytes went to the Drive and are gone
		return &zeroFile{SectionReader: io.NewSectionReader(zeros{}, 0, info.size), info: info}, nil
	}
}

func (s *speedTestFS) RemoveAll(ctx context.Context, name string) error {
	inside, ok := speedName(name)
	if !ok {
		return s.FS.RemoveAll(ctx, name)
	}
	s.mu.Lock()
	delete(s.written, inside)
	s.mu.Unlock()
	return nil
}

func (s *speedTestFS) Rename(ctx context.Context, oldName, newName string) error {
	from, okFrom := speedName(oldName)
	to, okTo := speedName(newName)
	if !okFrom && !okTo {
		return s.FS.Rename(ctx, oldName, newName)
	}
	if !okFrom || !okTo || from == "" || to == "" {
		return pathErr("rename", newName, os.ErrPermission)
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	info, ok := s.written[from]
	if !ok {
		return pathErr("rename", oldName, os.ErrNotExist)
	}
	delete(s.written, from)
	info.name = path.Base(to)
	s.written[to] = info
	return nil
}

func (s *speedTestFS) Mkdir(ctx context.Context, name string, perm os.FileMode) error {
	if _, ok := speedName(name); ok {
		return pathErr("mkdir", name, os.ErrPermission)
	}
	return s.FS.Mkdir(ctx, name, perm)
}

// speedSource reads the synthetic file like a real one.
type speedSource struct {
	client *api.Client
	size   int64
}

func (s speedSource) From(ctx context.Context, offset int64) (io.ReadCloser, string, error) {
	return s.client.SpeedTestFrom(ctx, s.size, offset)
}

func (s speedSource) Range(ctx context.Context, offset, length int64) ([]byte, string, error) {
	return s.client.SpeedTestRange(ctx, s.size, offset, length)
}

// speedWrite buffers a write like a real PUT, then uploads it in parallel
// chunks on Close (as real uploads do), to the discarding endpoint.
type speedWrite struct {
	*os.File
	ctx  context.Context
	fs   *speedTestFS
	name string
	req  *request
	err  error
}

func (w *speedWrite) Write(p []byte) (int, error) {
	n, err := w.File.Write(p)
	if err != nil && w.err == nil {
		w.err = err
	}
	return n, err
}

func (w *speedWrite) ReadFrom(r io.Reader) (int64, error) {
	n, err := w.File.ReadFrom(r)
	if err != nil && w.err == nil {
		w.err = err
	}
	return n, err
}

func (w *speedWrite) Stat() (fs.FileInfo, error) {
	st, err := w.File.Stat()
	if err != nil {
		return nil, err
	}
	return fileInfo{name: w.name, size: st.Size(), mtime: st.ModTime()}, nil
}

func (w *speedWrite) Readdir(int) ([]fs.FileInfo, error) { return nil, errNotSupported }

func (w *speedWrite) Close() error {
	defer os.Remove(w.File.Name())
	st, statErr := w.File.Stat()
	if err := w.File.Close(); err != nil || statErr != nil || w.err != nil {
		return errors.New("nothing was saved")
	}
	size := st.Size()
	// Like a real share: "._" and other hidden files stay on the Mac.
	if w.req.method == "PUT" && size > 0 && !localOnly(w.name) {
		if err := w.upload(size); err != nil {
			return w.fs.osErr("write", w.name, err)
		}
	}
	w.fs.mu.Lock()
	w.fs.written[w.name] = fileInfo{name: w.name, size: size, mtime: time.Now()}
	w.fs.mu.Unlock()
	return nil
}

func (w *speedWrite) upload(size int64) error {
	f, err := os.Open(w.File.Name())
	if err != nil {
		return err
	}
	defer f.Close()
	ctx, cancel := context.WithCancel(w.ctx)
	defer cancel()
	offsets := make(chan int64)
	errs := make(chan error, speedStreams)
	var wg sync.WaitGroup
	for i := 0; i < speedStreams; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for off := range offsets {
				n := min(speedChunk, size-off)
				if err := w.fs.client.SpeedTestUpload(ctx, io.NewSectionReader(f, off, n), n); err != nil {
					errs <- err
					cancel()
					return
				}
			}
		}()
	}
feed:
	for off := int64(0); off < size; off += speedChunk {
		select {
		case offsets <- off:
		case <-ctx.Done():
			break feed
		}
	}
	close(offsets)
	wg.Wait()
	close(errs)
	if err := <-errs; err != nil {
		return err
	}
	return w.ctx.Err()
}

// speedDir lists what the test wrote.
type speedDir struct {
	fs   *speedTestFS
	info fileInfo
	done bool
}

func (d *speedDir) Readdir(count int) ([]fs.FileInfo, error) {
	if d.done {
		if count > 0 {
			return nil, io.EOF
		}
		return nil, nil
	}
	d.done = true
	d.fs.mu.Lock()
	defer d.fs.mu.Unlock()
	out := make([]fs.FileInfo, 0, len(d.fs.written))
	for _, info := range d.fs.written {
		out = append(out, info)
	}
	return out, nil
}

func (d *speedDir) Stat() (fs.FileInfo, error)     { return d.info, nil }
func (d *speedDir) Close() error                   { return nil }
func (d *speedDir) Read([]byte) (int, error)       { return 0, errNotSupported }
func (d *speedDir) Write([]byte) (int, error)      { return 0, errNotSupported }
func (d *speedDir) Seek(int64, int) (int64, error) { return 0, errNotSupported }

// rootDir is the volume's top folder, plus the speed-test folder.
type rootDir struct {
	webdav.File
	extra fs.FileInfo
	added bool
}

func (r *rootDir) Readdir(count int) ([]fs.FileInfo, error) {
	entries, err := r.File.Readdir(count)
	if !r.added && (err == nil || err == io.EOF) {
		r.added = true
		return append(entries, r.extra), nil
	}
	return entries, err
}

// zeroFile stands in for a file the test wrote (its bytes were discarded).
type zeroFile struct {
	*io.SectionReader
	info fileInfo
}

func (z *zeroFile) Close() error                       { return nil }
func (z *zeroFile) Stat() (fs.FileInfo, error)         { return z.info, nil }
func (z *zeroFile) Readdir(int) ([]fs.FileInfo, error) { return nil, errNotSupported }
func (z *zeroFile) Write([]byte) (int, error)          { return 0, errNotSupported }

type zeros struct{}

func (zeros) ReadAt(p []byte, _ int64) (int, error) {
	clear(p)
	return len(p), nil
}
