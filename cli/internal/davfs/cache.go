package davfs

import (
	"strings"

	"github.com/neelsatyavolu/ugreen-drive/cli/internal/api"
)

// Listing cache upkeep. Writes update cached listings in place instead of
// dropping them all, so copying many files into a folder doesn't re-list it
// for every file (each listing is a round trip through the tunnel). A change
// also applies to a fetch of that listing under way (see fetchList), so a
// listing from before the change can't undo it. Callers of edit and drop
// hold f.mu.

// edit applies change to key's cached listing and to a fetch of it under way.
// change must return a new slice: readers may still hold the old one.
func (f *FS) edit(key string, change func([]api.Entry) []api.Entry) {
	if cached, ok := f.lists[key]; ok {
		f.lists[key] = cachedList{items: change(cached.items), at: cached.at}
	}
	if fl, ok := f.flights[key]; ok {
		fl.edits = append(fl.edits, change)
	}
}

// drop forgets key's listing, and the result of a fetch of it under way.
func (f *FS) drop(key string) {
	delete(f.lists, key)
	if fl, ok := f.flights[key]; ok {
		fl.dropped = true
		delete(f.flights, key)
	}
}

// without returns items minus the entry called name.
func without(items []api.Entry, name string) []api.Entry {
	out := make([]api.Entry, 0, len(items))
	for _, item := range items {
		if item.Name != name {
			out = append(out, item)
		}
	}
	return out
}

// cachePut records an entry the Drive just returned (upload, mkdir) in its
// parent's listing.
func (f *FS) cachePut(t target, e api.Entry) {
	dir, _ := api.SplitPath(t.rel)
	f.mu.Lock()
	defer f.mu.Unlock()
	f.edit(t.with(dir).key(), func(items []api.Entry) []api.Entry {
		return append(without(items, e.Name), e)
	})
}

// cacheForget removes t from its parent's listing and forgets listings of t
// and anything below it (a deleted or moved folder).
func (f *FS) cacheForget(t target) {
	dir, base := api.SplitPath(t.rel)
	prefix := t.key()
	below := func(key string) bool { return key == prefix || strings.HasPrefix(key, prefix+"/") }
	f.mu.Lock()
	defer f.mu.Unlock()
	f.edit(t.with(dir).key(), func(items []api.Entry) []api.Entry { return without(items, base) })
	for key := range f.lists {
		if below(key) {
			f.drop(key)
		}
	}
	for key := range f.flights {
		if below(key) {
			f.drop(key)
		}
	}
}

// cacheDropParent forgets t's parent listing (its state is unknown now).
func (f *FS) cacheDropParent(t target) {
	dir, _ := api.SplitPath(t.rel)
	f.mu.Lock()
	f.drop(t.with(dir).key())
	f.mu.Unlock()
}

// cacheMoved updates the cache after src was renamed/moved to dst.
func (f *FS) cacheMoved(src, dst target) {
	f.cacheForget(src)
	f.cacheForget(dst)
	f.cacheDropParent(dst) // re-list to learn the moved item's details
}
