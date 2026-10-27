package davfs

import (
	"container/list"
	"context"
	"fmt"
	"path"
	"strings"
	"sync"
	"time"

	"github.com/neelsatyavolu/ugreen-drive/cli/internal/api"
)

// Small reads go through a cache of 1 MiB blocks. Quick Look makes a video
// thumbnail with a chain of small reads through the index at the end of the
// file (13 reads of 32 B–110 KB, each waiting for the last: ~1 s of round
// trips per video), then reads one frame. Fetching the block around each read
// turns the chain into one or two requests, and making the thumbnail again
// (scrolling back, a second window) costs nothing. Opening a video also
// prefetches its last blocks, where .mov/.mp4 files usually keep the index.
//
// Blocks are keyed by the file's share, path, size and modification time from
// a fresh listing (see remoteFile.cacheKey), so a changed file never reuses
// them; every block also carries its download version, which a read checks
// like any other bytes.
const (
	blockSize      = 1 << 20
	cacheBlocks    = 64 // 64 MiB
	prefetchBlocks = 2
	blockTimeout   = 30 * time.Second
)

// videoExts are files whose index Quick Look reads from the end.
var videoExts = map[string]bool{".mov": true, ".mp4": true, ".m4v": true, ".3gp": true, ".qt": true}

func isVideo(name string) bool { return videoExts[strings.ToLower(path.Ext(name))] }

type blockID struct {
	file  string // remoteFile.cacheKey
	index int64
}

type block struct {
	done    chan struct{}
	data    []byte
	version string
	err     error
	elem    *list.Element
}

type blockCache struct {
	mu     sync.Mutex
	blocks map[blockID]*block
	lru    *list.List // of blockID, most recent first
}

func newBlockCache() *blockCache {
	return &blockCache{blocks: map[blockID]*block{}, lru: list.New()}
}

// get returns block index of the file, fetching it once however many readers
// want it. A failed fetch isn't kept.
func (c *blockCache) get(ctx context.Context, src byteSource, file string, size, index int64) *block {
	id := blockID{file, index}
	c.mu.Lock()
	if b, ok := c.blocks[id]; ok {
		c.lru.MoveToFront(b.elem)
		c.mu.Unlock()
		return b
	}
	b := &block{done: make(chan struct{})}
	b.elem = c.lru.PushFront(id)
	c.blocks[id] = b
	for c.lru.Len() > cacheBlocks {
		oldest := c.lru.Back()
		delete(c.blocks, oldest.Value.(blockID))
		c.lru.Remove(oldest)
	}
	c.mu.Unlock()
	go func() {
		// Not the asking request's context: others may be waiting on this block.
		fetchCtx, cancel := context.WithTimeout(api.Warm(context.WithoutCancel(ctx)), blockTimeout)
		defer cancel()
		off := index * blockSize
		b.data, b.version, b.err = src.Range(fetchCtx, off, min(blockSize, size-off))
		if b.err != nil {
			c.mu.Lock()
			if c.blocks[id] == b {
				delete(c.blocks, id)
				c.lru.Remove(b.elem)
			}
			c.mu.Unlock()
		}
		close(b.done)
	}()
	return b
}

// read returns bytes [from, end) of the file and their version, from blocks
// fetched in parallel (cached or not yet). The blocks must all be one version.
func (c *blockCache) read(ctx context.Context, src byteSource, file string, size, from, end int64) ([]byte, string, error) {
	var blocks []*block
	for i := from / blockSize; i*blockSize < end; i++ {
		blocks = append(blocks, c.get(ctx, src, file, size, i))
	}
	out := make([]byte, 0, end-from)
	version := ""
	for n, b := range blocks {
		select {
		case <-b.done:
		case <-ctx.Done():
			return nil, "", ctx.Err()
		}
		if b.err != nil {
			return nil, "", b.err
		}
		if version != "" && b.version != version {
			return nil, "", fmt.Errorf("%w (block %d)", errChanged, n)
		}
		version = b.version
		blockStart := (from/blockSize + int64(n)) * blockSize
		lo, hi := max(from, blockStart)-blockStart, min(end, blockStart+int64(len(b.data)))-blockStart
		if lo > hi || hi > int64(len(b.data)) {
			return nil, "", errChanged // the block is shorter than the listing said
		}
		out = append(out, b.data[lo:hi]...)
	}
	return out, version, nil
}

// prefetch starts fetching the last blocks of a video, for Quick Look.
func (c *blockCache) prefetch(ctx context.Context, src byteSource, file string, size int64) {
	last := (size - 1) / blockSize
	for i := max(0, last-prefetchBlocks+1); i <= last; i++ {
		c.get(ctx, src, file, size, i)
	}
}
