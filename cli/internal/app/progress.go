package app

import (
	"fmt"
	"io"
	"sync"
	"time"
)

// progress draws one self-updating status line on a terminal. When disabled
// (not a TTY, or --json) every method is a no-op except note, which is silent too.
type progress struct {
	mu         sync.Mutex
	w          io.Writer
	enabled    bool
	totalFiles int
	doneFiles  int
	totalBytes int64
	doneBytes  int64
	current    string
	started    time.Time
	stop       chan struct{}
	done       chan struct{}
}

func (r *runner) newProgress(files int, bytes int64) *progress {
	p := &progress{
		w: r.env.Stderr, enabled: r.env.StderrIsTTY && !r.g.json,
		totalFiles: files, totalBytes: bytes, started: time.Now(),
	}
	if p.enabled {
		p.stop, p.done = make(chan struct{}), make(chan struct{})
		go p.loop()
	}
	return p
}

func (p *progress) loop() {
	defer close(p.done)
	tick := time.NewTicker(250 * time.Millisecond)
	defer tick.Stop()
	for {
		select {
		case <-tick.C:
			p.mu.Lock()
			p.draw()
			p.mu.Unlock()
		case <-p.stop:
			return
		}
	}
}

// draw renders the line; the caller holds p.mu.
func (p *progress) draw() {
	elapsed := time.Since(p.started).Seconds()
	speed := 0.0
	if elapsed > 0 {
		speed = float64(p.doneBytes) / elapsed
	}
	eta := "…"
	if speed > 0 && p.totalBytes > p.doneBytes {
		eta = (time.Duration(float64(p.totalBytes-p.doneBytes)/speed) * time.Second).Round(time.Second).String() + " left"
	}
	fmt.Fprintf(p.w, "\r\033[K%d/%d files · %s / %s · %s/s · %s · %s",
		p.doneFiles, p.totalFiles, formatSize(p.doneBytes), formatSize(p.totalBytes),
		formatSize(int64(speed)), eta, p.current)
}

func (p *progress) add(n int64) {
	p.mu.Lock()
	p.doneBytes += n
	p.mu.Unlock()
}

func (p *progress) fileStarted(name string) {
	p.mu.Lock()
	p.current = name
	p.mu.Unlock()
}

func (p *progress) fileDone() {
	p.mu.Lock()
	p.doneFiles++
	p.mu.Unlock()
}

// note prints a message on its own line above the status line.
func (p *progress) note(format string, args ...any) {
	if !p.enabled {
		return
	}
	p.mu.Lock()
	defer p.mu.Unlock()
	fmt.Fprintf(p.w, "\r\033[K"+format+"\n", args...)
	p.draw()
}

func (p *progress) finish() {
	if !p.enabled {
		return
	}
	close(p.stop)
	<-p.done
	fmt.Fprint(p.w, "\r\033[K")
}
