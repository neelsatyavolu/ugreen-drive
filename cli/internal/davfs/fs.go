// Package davfs serves a user's Drive shares as a WebDAV file system so Finder
// can mount them through a local server (`ugd webdav`, used by the Mac app).
//
// Layout: the WebDAV root lists the shares the account can open, one folder
// each; everything below a share folder maps onto the Drive API.
//
// Names the Drive hides from listings (macOS "._" AppleDouble files,
// .DS_Store, *.tmp …) never reach the Drive. They live in a local store for
// as long as the server runs, so Finder and apps that save through a temp
// file still see what they wrote.
package davfs

import (
	"context"
	"errors"
	"fmt"
	"os"
	"path"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	"github.com/neelsatyavolu/ugreen-drive/cli/internal/api"
)

const (
	listTTL   = 10 * time.Second // Finder re-lists folders constantly; writes update the cache in place
	listStale = 2 * time.Minute  // until then an older listing is served while a fresh one loads
	sharesTTL = time.Minute
	// fetchTimeout bounds a listing or share-list fetch that runs on its own
	// (shared by several requests, or refreshing in the background).
	fetchTimeout = time.Minute
	// pendingGrace: how long a new, still empty file waits for its content
	// before the empty file is created on the Drive (see writeFile.Close).
	pendingGrace = 2 * time.Second
)

// FS implements webdav.FileSystem on top of the Drive API.
type FS struct {
	client *api.Client
	local  *localStore
	start  time.Time

	// OnSignedOut is called when the Drive rejects the token (optional).
	OnSignedOut func()
	// OnUpload is called as uploads start, progress and finish (optional).
	OnUpload func(Upload)
	// OnWriting reports how many files are open for writing (a Finder copy in
	// progress, before its upload starts), so the app never restarts mid-copy.
	OnWriting func(open int)
	// PendingGrace is how long a new empty file waits for its content before
	// it is created empty on the Drive (default pendingGrace).
	PendingGrace time.Duration

	mu         sync.Mutex
	lists      map[string]cachedList
	flights    map[string]*flight // listings being fetched
	shares     []api.Share
	sharesAt   time.Time
	uploadSeq  int64
	writing    int
	sharesMu   sync.Mutex  // serializes share-list fetches
	refreshing atomic.Bool // a background share-list refresh is running
	locks      *pendingLocks
	waiting    map[string]*waiter // new empty files waiting out PendingGrace
	readSlots  chan struct{}      // downloads all reads share (totalReadStreams)
	blocks     *blockCache        // small reads (see blocks.go)

	sharesTTL, listTTL, listStale time.Duration // the constants; tests shorten them
}

// flight is one folder listing being fetched; everyone who needs it waits
// for the same request. Local changes made while it runs (an upload landing)
// are replayed onto its result, which a listing from before must not undo.
type flight struct {
	done    chan struct{}
	items   []api.Entry
	err     error
	started time.Time
	edits   []func([]api.Entry) []api.Entry // local changes since it started
	dropped bool                            // the folder's state became unknown: don't cache
}

type cachedList struct {
	items []api.Entry
	at    time.Time
}

// New returns a file system for client's account. Local-only files are kept
// in tempDir, which the caller creates and removes.
func New(client *api.Client, tempDir string) *FS {
	return &FS{
		client:       client,
		local:        newLocalStore(tempDir),
		start:        time.Now(),
		lists:        map[string]cachedList{},
		flights:      map[string]*flight{},
		waiting:      map[string]*waiter{},
		readSlots:    make(chan struct{}, totalReadStreams),
		blocks:       newBlockCache(),
		PendingGrace: pendingGrace,
		sharesTTL:    sharesTTL,
		listTTL:      listTTL,
		listStale:    listStale,
	}
}

// SetShares fills the share list with an /api/me answer the caller already
// has (the helper checks the sign-in before it starts serving).
func (f *FS) SetShares(shares []api.Share) {
	if shares == nil {
		shares = []api.Share{}
	}
	f.mu.Lock()
	f.shares, f.sharesAt = shares, time.Now()
	f.mu.Unlock()
}

// target is a WebDAV name resolved to a share and a path inside it.
type target struct {
	share  api.Share // zero value = the root that lists shares
	rel    string    // path inside the share; "" = the share itself
	locked bool      // a locked personal folder (only its root and LockedNote)
}

func (t target) isRoot() bool { return t.share.ID == "" }

func (t target) key() string { return t.share.ID + "\x00" + t.rel }

func (t target) base() string { return path.Base("/" + t.rel) }

func (t target) with(rel string) target { return target{share: t.share, rel: rel} }

// shareName is the folder name a share gets at the WebDAV root.
func shareName(s api.Share) string {
	name := strings.ReplaceAll(strings.TrimSpace(s.Name), "/", "-")
	if name == "" {
		return strings.ReplaceAll(s.ID, "/", "-")
	}
	return name
}

// localOnly mirrors fsops._is_hidden_entry on the Drive, plus "._" files:
// anything the Drive would hide from listings stays on this Mac.
func localOnly(name string) bool {
	switch name {
	case ".DS_Store", "Thumbs.db", "desktop.ini":
		return true
	}
	if strings.HasPrefix(name, "._") || strings.HasPrefix(name, ".ifd-") {
		return true
	}
	lower := strings.ToLower(name)
	for _, suffix := range []string{".ug-tmp", ".ugtmp", ".tmp", ".partial", ".crdownload"} {
		if strings.HasSuffix(lower, suffix) {
			return true
		}
	}
	return false
}

// errLocked: an encrypted personal folder that isn't unlocked. Finder shows it
// as "no permission"; the Mac app offers Unlock.
var errLocked = fmt.Errorf("%w: personal folder is locked", os.ErrPermission)

// lockedRecheck limits how often a locked share makes us re-read the share
// list (to notice an unlock right away without asking on every request).
const lockedRecheck = time.Second

func pathErr(op, name string, err error) error {
	return &os.PathError{Op: op, Path: name, Err: err}
}

// osErr turns Drive API errors into the os errors x/net/webdav understands.
func (f *FS) osErr(op, name string, err error) error {
	var apiErr *api.Error
	if !errors.As(err, &apiErr) {
		return err
	}
	switch apiErr.Status {
	case 401:
		if f.OnSignedOut != nil {
			f.OnSignedOut()
		}
		return pathErr(op, name, fmt.Errorf("%w: %v", os.ErrPermission, err))
	case 403:
		return pathErr(op, name, fmt.Errorf("%w: %v", os.ErrPermission, err))
	case 404:
		return pathErr(op, name, os.ErrNotExist)
	case 423, 428: // encrypted personal folder is locked
		return pathErr(op, name, errLocked)
	case 409:
		return pathErr(op, name, os.ErrExist)
	}
	return err
}

func (f *FS) clientFor(s api.Share) *api.Client {
	c := *f.client
	c.Share = s.ID
	return &c
}

// listShares returns the share list. Once there is one it never waits: an
// old list is served while a fresh one loads (/api/me asks UGOS about
// personal folders, which can take seconds).
func (f *FS) listShares(ctx context.Context) ([]api.Share, error) {
	f.mu.Lock()
	shares, at := f.shares, f.sharesAt
	f.mu.Unlock()
	if shares == nil {
		return f.fetchShares(ctx, f.sharesTTL)
	}
	if time.Since(at) >= f.sharesTTL && f.refreshing.CompareAndSwap(false, true) {
		go func() {
			defer f.refreshing.Store(false)
			ctx, cancel := context.WithTimeout(context.Background(), fetchTimeout)
			defer cancel()
			f.fetchShares(ctx, f.sharesTTL)
		}()
	}
	return shares, nil
}

func (f *FS) fetchShares(ctx context.Context, maxAge time.Duration) ([]api.Share, error) {
	// One /api/me at a time: Finder's parallel requests share the answer.
	f.sharesMu.Lock()
	defer f.sharesMu.Unlock()
	f.mu.Lock()
	if f.shares != nil && time.Since(f.sharesAt) < maxAge {
		shares := f.shares
		f.mu.Unlock()
		return shares, nil
	}
	f.mu.Unlock()
	me, err := f.client.Me(ctx)
	if err != nil {
		return nil, f.osErr("stat", "/", err)
	}
	if !me.Authenticated {
		return nil, f.osErr("stat", "/", &api.Error{Status: 401})
	}
	shares := me.Shares
	if shares == nil {
		shares = []api.Share{}
	}
	f.mu.Lock()
	f.shares, f.sharesAt = shares, time.Now()
	f.mu.Unlock()
	return shares, nil
}

func (f *FS) resolve(ctx context.Context, name string) (target, error) {
	// CleanPath treats "\" as a separator, but on a Mac it is part of a
	// name; refuse such names rather than write somewhere else.
	if strings.Contains(name, "\\") {
		return target{}, pathErr("stat", name, os.ErrNotExist)
	}
	clean := api.CleanPath(name)
	if clean == "" {
		return target{}, nil
	}
	head, rest, _ := strings.Cut(clean, "/")
	shares, err := f.listShares(ctx)
	if err != nil {
		return target{}, err
	}
	for _, s := range shares {
		if shareName(s) != head {
			continue
		}
		if s.Locked {
			// It may have been unlocked (here, on the website or in the app).
			if fresh, err := f.fetchShares(ctx, lockedRecheck); err == nil {
				for _, again := range fresh {
					if again.ID == s.ID {
						s = again
					}
				}
			}
			if s.Locked {
				if rest == "" || rest == LockedNote {
					return target{share: s, rel: rest, locked: true}, nil
				}
				return target{}, pathErr("open", name, errLocked)
			}
		}
		return target{share: s, rel: rest}, nil
	}
	return target{}, pathErr("stat", name, os.ErrNotExist)
}

// list returns a Drive folder's entries, cached briefly. With stale (Finder
// showing a folder) a listing past its TTL is still served, for up to
// listStale, while a fresh one loads. Looking up one name never uses a stale
// listing: its size decides what a read serves, and whether a write is new.
func (f *FS) list(ctx context.Context, t target, stale bool) ([]api.Entry, error) {
	f.mu.Lock()
	cached, ok := f.lists[t.key()]
	f.mu.Unlock()
	age := time.Since(cached.at)
	switch {
	case ok && age < f.listTTL:
		return cached.items, nil
	case ok && stale && age < f.listStale:
		f.fetchList(t)
		return cached.items, nil
	}
	fl := f.fetchList(t)
	select {
	case <-fl.done:
		return fl.items, fl.err
	case <-ctx.Done():
		return nil, ctx.Err()
	}
}

// fetchList starts fetching t's listing, or joins the fetch already running.
func (f *FS) fetchList(t target) *flight {
	key := t.key()
	f.mu.Lock()
	defer f.mu.Unlock()
	if fl, ok := f.flights[key]; ok {
		return fl
	}
	fl := &flight{done: make(chan struct{}), started: time.Now()}
	f.flights[key] = fl
	go func() {
		ctx, cancel := context.WithTimeout(context.Background(), fetchTimeout)
		defer cancel()
		listing, err := f.clientFor(t.share).List(ctx, t.rel)
		if err != nil {
			err = f.osErr("readdir", t.rel, err)
		}
		f.mu.Lock()
		if f.flights[key] == fl {
			delete(f.flights, key)
		}
		items := listing.Items
		switch {
		case err == nil:
			for _, edit := range fl.edits {
				items = edit(items)
			}
			if !fl.dropped {
				f.lists[key] = cachedList{items: items, at: fl.started}
			}
		case !fl.dropped && (os.IsNotExist(err) || errors.Is(err, os.ErrPermission)):
			delete(f.lists, key) // gone, locked or forbidden: stop serving it
		}
		fl.items, fl.err = items, err
		f.mu.Unlock()
		close(fl.done)
	}()
	return fl
}

// node is a resolved, existing name.
type node struct {
	t     target
	info  fileInfo
	local *localEntry // set for local-only files and ghosts
}

func (f *FS) find(ctx context.Context, name string) (node, error) {
	t, err := f.resolve(ctx, name)
	if err != nil {
		return node{}, err
	}
	if t.isRoot() {
		return node{t: t, info: fileInfo{name: "/", dir: true, mtime: f.start}}, nil
	}
	if t.rel == "" {
		return node{t: t, info: fileInfo{name: shareName(t.share), dir: true, mtime: f.start}}, nil
	}
	if t.locked {
		return node{t: t, info: f.lockedNoteInfo()}, nil
	}
	if entry, ok := f.local.get(t.key()); ok {
		return node{t: t, info: entry.info(t.base()), local: &entry}, nil
	}
	if localOnly(t.base()) {
		return node{}, pathErr("stat", name, os.ErrNotExist)
	}
	dir, base := api.SplitPath(t.rel)
	items, err := f.list(ctx, t.with(dir), false)
	if err != nil {
		return node{}, err
	}
	for _, item := range items {
		if item.Name == base {
			return node{t: t, info: entryInfo(item)}, nil
		}
	}
	return node{}, pathErr("stat", name, os.ErrNotExist)
}

// Stat implements webdav.FileSystem.
func (f *FS) Stat(ctx context.Context, name string) (os.FileInfo, error) {
	n, err := f.find(ctx, name)
	if err != nil {
		return nil, err
	}
	return n.info, nil
}

// writable resolves name for a change inside a share the account can write.
func (f *FS) writable(ctx context.Context, op, name string) (target, error) {
	t, err := f.resolve(ctx, name)
	if err != nil {
		return target{}, err
	}
	if t.isRoot() || t.rel == "" || t.locked || !t.share.CanWrite {
		return target{}, pathErr(op, name, os.ErrPermission)
	}
	return t, nil
}

// Mkdir implements webdav.FileSystem.
func (f *FS) Mkdir(ctx context.Context, name string, _ os.FileMode) error {
	t, err := f.writable(ctx, "mkdir", name)
	if err != nil {
		return err
	}
	dir, base := api.SplitPath(t.rel)
	entry, err := f.clientFor(t.share).Mkdir(ctx, dir, base)
	if err != nil {
		f.cacheDropParent(t)
		return f.osErr("mkdir", name, err)
	}
	f.cachePut(t, entry)
	return nil
}

// RemoveAll implements webdav.FileSystem. Drive items go to the recycle bin.
func (f *FS) RemoveAll(ctx context.Context, name string) error {
	t, err := f.writable(ctx, "remove", name)
	if err != nil {
		return err
	}
	if f.replacedByMove(ctx, t) {
		return nil
	}
	f.local.removeTree(t.share.ID, t.rel)
	entry, isLocal := f.local.remove(t.key())
	remote := t.rel
	switch {
	case isLocal && entry.ghost == "":
		return nil
	case isLocal:
		remote = entry.ghost
	case localOnly(t.base()):
		return nil
	}
	if _, err := f.clientFor(t.share).Delete(ctx, remote); err != nil {
		if err := f.osErr("remove", name, err); !os.IsNotExist(err) {
			f.cacheDropParent(t.with(remote)) // it may still be there
			return err
		}
	}
	f.cacheForget(t.with(remote))
	return nil
}

// replacedByMove reports whether RemoveAll(t) is x/net/webdav clearing the
// destination of a MOVE whose source is a local temp file (an app's safe
// save). Rename then uploads over the document, which replaces it only once
// the upload succeeded; deleting it first would lose it if the upload failed.
func (f *FS) replacedByMove(ctx context.Context, t target) bool {
	req := requestOf(ctx)
	if req.method != "MOVE" || api.CleanPath(shareName(t.share)+"/"+t.rel) != req.moveDst || localOnly(t.base()) {
		return false
	}
	src, err := f.find(ctx, req.moveSrc)
	if err != nil || src.local == nil || src.local.ghost != "" || src.local.pending || src.t.share.ID != t.share.ID {
		return false
	}
	dst, err := f.find(ctx, shareName(t.share)+"/"+t.rel)
	return err == nil && !dst.info.dir && dst.local == nil
}

// Rename implements webdav.FileSystem (MOVE). Moves between shares aren't
// supported by the Drive, so they fail.
func (f *FS) Rename(ctx context.Context, oldName, newName string) error {
	src, err := f.writable(ctx, "rename", oldName)
	if err != nil {
		return err
	}
	dst, err := f.writable(ctx, "rename", newName)
	if err != nil {
		return err
	}
	if src.share.ID != dst.share.ID {
		return pathErr("rename", newName, fmt.Errorf("%w: can't move between shares", os.ErrPermission))
	}
	n, err := f.find(ctx, oldName)
	if err != nil {
		return err
	}
	client := f.clientFor(src.share)
	hidden := localOnly(dst.base())
	switch {
	case n.local != nil && n.local.pending:
		// Moving a file Finder hasn't written yet: it's an empty file now.
		f.local.remove(src.key())
		return f.createEmpty(ctx, dst, false)
	case n.local != nil && n.local.ghost == "" && hidden:
		f.local.move(src.key(), dst.key())
		return nil
	case n.local != nil && n.local.ghost == "":
		// An app saved to a temp name and is renaming it into place.
		if err := f.upload(ctx, dst, n.local.file, api.UploadOptions{}); err != nil {
			return err
		}
		f.local.remove(src.key())
		return nil
	case n.local != nil:
		if err := f.moveRemote(ctx, client, n.local.ghost, dst.rel); err != nil {
			f.cacheDropParent(src.with(n.local.ghost))
			f.cacheDropParent(dst)
			return f.osErr("rename", newName, err)
		}
		f.cacheMoved(src.with(n.local.ghost), dst)
		f.local.remove(src.key())
	default:
		if err := f.moveRemote(ctx, client, src.rel, dst.rel); err != nil {
			f.cacheDropParent(src)
			f.cacheDropParent(dst)
			return f.osErr("rename", newName, err)
		}
		f.cacheMoved(src, dst)
	}
	if n.info.dir {
		f.local.moveTree(src.share.ID, src.rel, dst.rel)
		// New files inside that wait for their content moved too.
		for _, rel := range f.local.pending(dst.share.ID, dst.rel)[dst.share.ID] {
			f.finishLater(dst.with(rel))
		}
	}
	if hidden {
		// The Drive hides the new name, so remember it or it would vanish.
		f.local.put(dst.key(), localEntry{ghost: dst.rel, size: n.info.size, mtime: n.info.mtime, dir: n.info.dir})
	}
	return nil
}

// moveRemote renames and/or moves a Drive item from one path to another.
func (f *FS) moveRemote(ctx context.Context, c *api.Client, from, to string) error {
	fromDir, fromName := api.SplitPath(from)
	toDir, toName := api.SplitPath(to)
	if fromName != toName {
		if err := c.Rename(ctx, from, toName); err != nil {
			return err
		}
		from = path.Join(fromDir, toName)
	}
	if fromDir == toDir {
		return nil
	}
	if err := c.Move(ctx, from, toDir); err != nil {
		if fromName != toName {
			_ = c.Rename(ctx, from, fromName) // best effort: put it back
		}
		return err
	}
	return nil
}

// finishPending creates name on the Drive, empty, if it is still a
// placeholder: Finder LOCKed a new name (or created it with an empty PUT) and
// never wrote it, e.g. `touch`.
func (f *FS) finishPending(name string) {
	if t, err := f.resolve(context.Background(), name); err == nil {
		f.finishTarget(t)
	}
}

func (f *FS) finishTarget(t target) {
	entry, ok := f.local.get(t.key())
	if !ok || !entry.pending {
		return
	}
	f.local.remove(t.key())
	ctx, cancel := context.WithTimeout(context.Background(), time.Minute)
	defer cancel()
	// Exists already (someone else made it) is fine; anything else is a file
	// Finder showed that never reached the Drive: report it.
	if err := f.createEmpty(ctx, t, true); err != nil && !os.IsExist(err) {
		f.reportFailed(t, err)
	}
}

// reportFailed reports an upload that failed with nobody waiting on it.
func (f *FS) reportFailed(t target, err error) {
	if f.OnUpload == nil {
		return
	}
	f.mu.Lock()
	f.uploadSeq++
	id := f.uploadSeq
	f.mu.Unlock()
	f.OnUpload(Upload{ID: id, Path: shareName(t.share) + "/" + t.rel, State: "failed", Error: err.Error()})
}

// waiter is a placeholder waiting out PendingGrace for its content.
type waiter struct {
	t     target
	timer *time.Timer
}

// finishLater finishes t's placeholder after PendingGrace, unless the
// content arrives first (the PUT clears the placeholder).
func (f *FS) finishLater(t target) {
	key := t.key()
	w := &waiter{t: t}
	f.mu.Lock()
	defer f.mu.Unlock()
	if old, ok := f.waiting[key]; ok {
		old.timer.Stop()
	}
	f.waiting[key] = w
	w.timer = time.AfterFunc(f.PendingGrace, func() {
		f.mu.Lock()
		current := f.waiting[key] == w
		if current {
			delete(f.waiting, key)
		}
		f.mu.Unlock()
		if current {
			f.finishTarget(t)
		}
	})
}

// stopWaiting cancels key's grace-period timer: its content is being written.
// If that write fails, the placeholder goes with it (see writeFile.Close).
func (f *FS) stopWaiting(key string) {
	f.mu.Lock()
	defer f.mu.Unlock()
	if w, ok := f.waiting[key]; ok {
		w.timer.Stop()
		delete(f.waiting, key)
	}
}

// finishLaterName is finishLater for a WebDAV name (an UNLOCK).
func (f *FS) finishLaterName(name string) {
	if t, err := f.resolve(context.Background(), name); err == nil {
		f.finishLater(t)
	}
}

// createEmpty uploads an empty file to t (only if missing, when mustNotExist).
func (f *FS) createEmpty(ctx context.Context, t target, mustNotExist bool) error {
	tmp, err := f.local.tempFile()
	if err != nil {
		return err
	}
	tmp.Close()
	defer os.Remove(tmp.Name())
	opts := api.UploadOptions{}
	if mustNotExist {
		m := api.MustNotExist
		opts.ExpectMtimeNS = &m
	}
	return f.upload(ctx, t, tmp.Name(), opts)
}

// FinishPending creates every file Finder LOCKed or created but never wrote
// (on shutdown).
func (f *FS) FinishPending() {
	if f.locks != nil {
		f.locks.finishAll()
	}
	f.mu.Lock()
	waiting := make([]*waiter, 0, len(f.waiting))
	for key, w := range f.waiting {
		w.timer.Stop()
		waiting = append(waiting, w)
		delete(f.waiting, key)
	}
	shares := f.shares
	f.mu.Unlock()
	for _, w := range waiting {
		f.finishTarget(w.t)
	}
	// Anything still waiting for content (e.g. LOCKed inside a folder that
	// was renamed before its UNLOCK).
	for id, rels := range f.local.pending("", "") {
		for _, s := range shares {
			if s.ID != id {
				continue
			}
			for _, rel := range rels {
				f.finishTarget(target{share: s, rel: rel})
			}
		}
	}
}
