package davfs

import (
	"os"
	"strings"
	"sync"
	"time"
)

// localEntry is a name that exists only on this Mac: either a local-only file
// (content in a temp file) or a "ghost", a Drive item renamed to a name the
// Drive hides from listings (so it can still be found, read and deleted).
type localEntry struct {
	file    string // temp file with the content; "" for a ghost or pending file
	ghost   string // share-relative Drive path of a ghost
	pending bool   // a new file Finder has LOCKed but not written yet
	size    int64
	mtime   time.Time
	dir     bool
}

func (e localEntry) info(name string) fileInfo {
	return fileInfo{name: name, size: e.size, mtime: e.mtime, dir: e.dir}
}

type localStore struct {
	dir     string
	mu      sync.Mutex
	entries map[string]localEntry // target key → entry
}

func newLocalStore(dir string) *localStore {
	return &localStore{dir: dir, entries: map[string]localEntry{}}
}

func (s *localStore) get(key string) (localEntry, bool) {
	s.mu.Lock()
	defer s.mu.Unlock()
	e, ok := s.entries[key]
	return e, ok
}

// put stores e under key, discarding whatever was there.
func (s *localStore) put(key string, e localEntry) {
	s.mu.Lock()
	old, ok := s.entries[key]
	s.entries[key] = e
	s.mu.Unlock()
	if ok && old.file != "" && old.file != e.file {
		os.Remove(old.file)
	}
}

func (s *localStore) move(from, to string) {
	s.mu.Lock()
	e, ok := s.entries[from]
	delete(s.entries, from)
	s.mu.Unlock()
	if ok {
		s.put(to, e)
	}
}

// remove forgets key (deleting its temp file) and returns what it was.
func (s *localStore) remove(key string) (localEntry, bool) {
	s.mu.Lock()
	e, ok := s.entries[key]
	delete(s.entries, key)
	s.mu.Unlock()
	if ok && e.file != "" {
		os.Remove(e.file)
	}
	return e, ok
}

// removeTree forgets everything below dir in a share.
func (s *localStore) removeTree(shareID, dir string) {
	prefix := shareID + "\x00" + dir + "/"
	s.mu.Lock()
	var files []string
	for key, e := range s.entries {
		if strings.HasPrefix(key, prefix) {
			delete(s.entries, key)
			if e.file != "" {
				files = append(files, e.file)
			}
		}
	}
	s.mu.Unlock()
	for _, file := range files {
		os.Remove(file)
	}
}

// pending returns the share-relative paths of every placeholder (a new file
// not written yet) in shareID below dir ("" = the whole share), or in every
// share when shareID is "" (map: share ID → paths).
func (s *localStore) pending(shareID, dir string) map[string][]string {
	out := map[string][]string{}
	s.mu.Lock()
	defer s.mu.Unlock()
	for key, e := range s.entries {
		id, rel, _ := strings.Cut(key, "\x00")
		if !e.pending || (shareID != "" && id != shareID) || (dir != "" && !strings.HasPrefix(rel, dir+"/")) {
			continue
		}
		out[id] = append(out[id], rel)
	}
	return out
}

// moveTree re-keys everything below from to below to (a renamed folder).
func (s *localStore) moveTree(shareID, from, to string) {
	prefix := shareID + "\x00" + from + "/"
	s.mu.Lock()
	defer s.mu.Unlock()
	moved := map[string]localEntry{}
	for key, e := range s.entries {
		rest, ok := strings.CutPrefix(key, prefix)
		if !ok {
			continue
		}
		if ghostRest, ok := strings.CutPrefix(e.ghost, from+"/"); ok {
			e.ghost = to + "/" + ghostRest
		}
		moved[shareID+"\x00"+to+"/"+rest] = e
		delete(s.entries, key)
	}
	for key, e := range moved {
		if old, ok := s.entries[key]; ok && old.file != "" && old.file != e.file {
			os.Remove(old.file)
		}
		s.entries[key] = e
	}
}

// children returns the entries directly inside dir, by name.
func (s *localStore) children(shareID, dir string) map[string]localEntry {
	prefix := shareID + "\x00"
	if dir != "" {
		prefix += dir + "/"
	}
	out := map[string]localEntry{}
	s.mu.Lock()
	defer s.mu.Unlock()
	for key, e := range s.entries {
		name, ok := strings.CutPrefix(key, prefix)
		if ok && name != "" && !strings.Contains(name, "/") {
			out[name] = e
		}
	}
	return out
}

func (s *localStore) tempFile() (*os.File, error) {
	return os.CreateTemp(s.dir, "put-*")
}
