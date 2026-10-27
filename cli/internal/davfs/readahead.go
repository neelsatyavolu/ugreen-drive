package davfs

import (
	"context"
	"errors"
	"io"
	"sync/atomic"

	"github.com/neelsatyavolu/ugreen-drive/cli/internal/api"
)

// Parallel reads: one HTTP stream tops out well below what several do
// (through the tunnel ~45 vs ~150 MB/s), so a large read fetches its range as
// readChunk pieces, readStreams at a time, and hands them out in order. At
// most readWindow chunks are fetched ahead of the reader (48 MiB). The first
// chunks are small (256 KiB, then 1 MiB) so the first bytes arrive in about a
// round trip. A read of up to smallRead bytes is a single request.
//
// A read ramps up like TCP slow start: after its first two chunks it keeps no
// more fetched ahead of the reader than the reader has already taken. Finder opens every video in a
// folder to make thumbnails and reads a little of each; without the ramp each
// open started 6 × 4 MiB downloads that were thrown away (1.6 GB fetched for
// 15 thumbnails). A copy still reaches full speed within a few MiB. All reads
// together share totalReadStreams downloads (the small first chunk doesn't
// wait), so a folder of thumbnails can't swamp the link and stall listings.
//
// Every chunk must come from the same version of the file (ETag, Last-Modified,
// size) as the bytes already read; otherwise the read fails instead of
// stitching an old and a new version together.
const (
	readChunk        = 4 << 20
	readStreams      = 6
	readWindow       = 12
	smallRead        = readChunk
	totalReadStreams = 8
)

// chunkSize is the length of the i-th chunk of a parallel read.
func chunkSize(i int) int64 {
	switch i {
	case 0:
		return 256 << 10
	case 1:
		return 1 << 20
	}
	return readChunk
}

// errChanged: the file was replaced on the Drive in the middle of a read.
var errChanged = errors.New("the file changed on the Drive while it was being read; copy it again")

type chunkResult struct {
	data    []byte
	version string
	err     error
}

// readAhead streams a file from offset `from` using parallel ranged requests.
type readAhead struct {
	cancel   context.CancelFunc
	results  []chan chunkResult // one per chunk, in file order
	window   chan struct{}      // limits chunks fetched ahead of the reader
	next     int                // next chunk to hand out
	cur      []byte             // unread part of the current chunk
	pos      int64              // file offset of cur[0]
	version  string             // every chunk must match this version
	size     int64              // ... and name this total size
	taken    atomic.Int64       // bytes handed to the reader (the ramp)
	tookMore chan struct{}      // nudges the scheduler when taken grows
}

// byteSource is where a remoteFile's bytes come from: a Drive file, or the
// speed-test file (so the speed test reads exactly the way Finder does).
type byteSource interface {
	From(ctx context.Context, offset int64) (io.ReadCloser, string, error)
	Range(ctx context.Context, offset, length int64) ([]byte, string, error)
}

// driveFile reads a file on the Drive.
type driveFile struct {
	client *api.Client
	rel    string
}

func (d driveFile) From(ctx context.Context, offset int64) (io.ReadCloser, string, error) {
	return d.client.DownloadVersionFrom(ctx, d.rel, offset)
}

func (d driveFile) Range(ctx context.Context, offset, length int64) ([]byte, string, error) {
	return d.client.DownloadRange(ctx, d.rel, offset, length)
}

// startReadAhead fetches bytes [from, end) of src in parallel chunks. slots,
// if set, is shared by every read (see totalReadStreams).
func startReadAhead(parent context.Context, src byteSource, from, end int64, version string, size int64, slots chan struct{}) *readAhead {
	ctx, cancel := context.WithCancel(parent)
	var spans [][2]int64 // offset, length
	for off := from; off < end; {
		size := min(chunkSize(len(spans)), end-off)
		spans = append(spans, [2]int64{off, size})
		off += size
	}
	r := &readAhead{
		cancel:   cancel,
		results:  make([]chan chunkResult, len(spans)),
		window:   make(chan struct{}, readWindow),
		pos:      from,
		version:  version,
		size:     size,
		tookMore: make(chan struct{}, 1),
	}
	for i := range r.results {
		r.results[i] = make(chan chunkResult, 1)
	}
	go func() {
		streams := make(chan struct{}, readStreams)
		for i, span := range spans {
			select {
			case r.window <- struct{}{}: // freed when the reader takes a chunk
			case <-ctx.Done():
				return
			}
			// Ramp: past the first two chunks, start one only while what is
			// already fetched ahead of the reader is no more than it has read.
			for i >= 2 && span[0]-from > 2*r.taken.Load() {
				select {
				case <-r.tookMore:
				case <-ctx.Done():
					return
				}
			}
			select {
			case streams <- struct{}{}:
			case <-ctx.Done():
				return
			}
			shared := slots != nil && i > 0
			if shared {
				select {
				case slots <- struct{}{}:
				case <-ctx.Done():
					<-streams
					return
				}
			}
			go func() {
				defer func() {
					if shared {
						<-slots
					}
					<-streams
				}()
				chunkCtx := ctx
				if i == 0 {
					chunkCtx = api.Warm(ctx) // the first bytes: no handshake wait
				}
				data, version, err := src.Range(chunkCtx, span[0], span[1])
				r.results[i] <- chunkResult{data, version, err}
			}()
		}
	}()
	return r
}

// Read hands out the file's bytes in order.
func (r *readAhead) Read(ctx context.Context, p []byte) (int, error) {
	for len(r.cur) == 0 {
		if r.next >= len(r.results) {
			return 0, io.EOF
		}
		var res chunkResult
		select {
		case res = <-r.results[r.next]:
		case <-ctx.Done():
			return 0, ctx.Err()
		}
		<-r.window
		r.next++
		if res.err != nil {
			return 0, res.err
		}
		if r.version == "" {
			if size, ok := versionSize(res.version); ok && size != r.size {
				return 0, errChanged
			}
			r.version = res.version
		} else if res.version != r.version {
			return 0, errChanged
		}
		r.cur = res.data
	}
	n := copy(p, r.cur)
	r.cur = r.cur[n:]
	r.pos += int64(n)
	r.taken.Add(int64(n))
	select {
	case r.tookMore <- struct{}{}:
	default:
	}
	return n, nil
}

func (r *readAhead) Close() { r.cancel() }

// rangeUnsupported reports whether the Drive answered ranges with whole files.
func rangeUnsupported(err error) bool { return errors.Is(err, api.ErrNoRange) }
