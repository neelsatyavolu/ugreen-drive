package transfer

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strings"
	"time"
)

// PieceSize is the Drive's fingerprint piece size (8 MiB).
const PieceSize = 8 << 20

// Fingerprint is the Drive's content fingerprint: SHA-256 over the
// concatenated SHA-256 digests of each 8 MiB piece (fsops.upload_fingerprint).
func Fingerprint(path string) (string, error) {
	f, err := os.Open(path)
	if err != nil {
		return "", err
	}
	defer f.Close()
	outer := sha256.New()
	buf := make([]byte, PieceSize)
	for {
		n, err := io.ReadFull(f, buf)
		if n > 0 {
			sum := sha256.Sum256(buf[:n])
			outer.Write(sum[:])
		}
		if errors.Is(err, io.EOF) || errors.Is(err, io.ErrUnexpectedEOF) {
			break
		}
		if err != nil {
			return "", fmt.Errorf("fingerprint %s: %w", path, err)
		}
	}
	return hex.EncodeToString(outer.Sum(nil)), nil
}

// State is a chunked upload the Drive can resume.
type State struct {
	UploadID    string    `json:"upload_id"`
	ChunkSize   int64     `json:"chunk_size"`
	TotalChunks int       `json:"total_chunks"`
	SavedAt     time.Time `json:"saved_at"`
}

// ResumeKey identifies one upload of one local file version to one Drive path.
// Any change to the local file (size or mtime) gives a new key.
func ResumeKey(server, share, remote, local string, size, modTime int64) string {
	sum := sha256.Sum256([]byte(strings.Join([]string{
		server, share, remote, local, fmt.Sprint(size), fmt.Sprint(modTime),
	}, "\x00")))
	return hex.EncodeToString(sum[:16])
}

// Store keeps resumable-upload state as small JSON files in Dir.
type Store struct {
	Dir string
}

func (s Store) path(key string) string { return filepath.Join(s.Dir, key+".json") }

// Load returns the saved state for key, if any.
func (s Store) Load(key string) (State, bool) {
	var st State
	raw, err := os.ReadFile(s.path(key))
	if err != nil || json.Unmarshal(raw, &st) != nil || st.UploadID == "" {
		return State{}, false
	}
	return st, true
}

// Save records state for key (owner-only file).
func (s Store) Save(key string, st State) error {
	if st.SavedAt.IsZero() {
		st.SavedAt = time.Now()
	}
	if err := os.MkdirAll(s.Dir, 0o700); err != nil {
		return err
	}
	raw, err := json.Marshal(st)
	if err != nil {
		return err
	}
	tmp, err := os.CreateTemp(s.Dir, ".state-*")
	if err != nil {
		return err
	}
	defer os.Remove(tmp.Name())
	if _, err := tmp.Write(raw); err != nil {
		tmp.Close()
		return err
	}
	if err := tmp.Close(); err != nil {
		return err
	}
	if err := os.Chmod(tmp.Name(), 0o600); err != nil {
		return err
	}
	return os.Rename(tmp.Name(), s.path(key))
}

// Delete forgets key.
func (s Store) Delete(key string) { os.Remove(s.path(key)) }

// Prune removes state older than maxAge (the Drive drops sessions after 24 h).
func (s Store) Prune(maxAge time.Duration) {
	entries, err := os.ReadDir(s.Dir)
	if err != nil {
		return
	}
	for _, e := range entries {
		key, ok := strings.CutSuffix(e.Name(), ".json")
		if !ok {
			continue
		}
		if st, ok := s.Load(key); !ok || time.Since(st.SavedAt) > maxAge {
			s.Delete(key)
		}
	}
}
