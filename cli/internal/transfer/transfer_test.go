package transfer

import (
	"crypto/sha256"
	"encoding/hex"
	"os"
	"path/filepath"
	"runtime"
	"sort"
	"testing"
	"time"
)

func write(t *testing.T, path, data string) {
	t.Helper()
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, []byte(data), 0o644); err != nil {
		t.Fatal(err)
	}
}

func TestSkipMatchesDriveJunkFilter(t *testing.T) {
	for _, name := range []string{".DS_Store", "Thumbs.db", "desktop.ini", "._clip.mov", ".ifd-x",
		"a.ug-tmp", "a.UGTMP", "a.tmp", "a.partial", "a.crdownload", ".Spotlight-V100", ".Trashes", ".fseventsd"} {
		if !Skip(name) {
			t.Errorf("%s should be skipped", name)
		}
	}
	for _, name := range []string{"clip.mov", ".env.example", "notes.md", "#recycle"} {
		if Skip(name) {
			t.Errorf("%s should be kept", name)
		}
	}
}

func TestCollectFilesAndFolders(t *testing.T) {
	root := t.TempDir()
	write(t, filepath.Join(root, "single.txt"), "s")
	write(t, filepath.Join(root, "Footage", "a.mov"), "aa")
	write(t, filepath.Join(root, "Footage", "Day 2", "b.mov"), "bbb")
	write(t, filepath.Join(root, "Footage", ".DS_Store"), "junk")
	if err := os.MkdirAll(filepath.Join(root, "Footage", "Empty"), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink(filepath.Join(root, "single.txt"), filepath.Join(root, "Footage", "link.txt")); err != nil {
		t.Fatal(err)
	}

	plan, err := Collect([]string{filepath.Join(root, "single.txt"), filepath.Join(root, "Footage")}, "Documents/Ep1", true)
	if err != nil {
		t.Fatal(err)
	}
	var got []string
	for _, f := range plan.Files {
		got = append(got, f.Remote)
	}
	sort.Strings(got)
	want := []string{"Documents/Ep1/Footage/Day 2/b.mov", "Documents/Ep1/Footage/a.mov", "Documents/Ep1/single.txt"}
	if len(got) != len(want) {
		t.Fatalf("files %v, want %v", got, want)
	}
	for i := range want {
		if got[i] != want[i] {
			t.Fatalf("files %v, want %v", got, want)
		}
	}
	sort.Strings(plan.Dirs)
	wantDirs := []string{"Documents/Ep1", "Documents/Ep1/Footage", "Documents/Ep1/Footage/Day 2", "Documents/Ep1/Footage/Empty"}
	if len(plan.Dirs) != len(wantDirs) {
		t.Fatalf("dirs %v, want %v", plan.Dirs, wantDirs)
	}
	for i := range wantDirs {
		if plan.Dirs[i] != wantDirs[i] {
			t.Fatalf("dirs %v, want %v", plan.Dirs, wantDirs)
		}
	}
	if len(plan.Symlinks) != 1 {
		t.Fatalf("symlinks %v", plan.Symlinks)
	}
}

func TestCollectRejectsFolderWithoutRecursive(t *testing.T) {
	root := t.TempDir()
	write(t, filepath.Join(root, "Footage", "a.mov"), "a")
	if _, err := Collect([]string{filepath.Join(root, "Footage")}, "Documents", false); err == nil {
		t.Fatal("folder without -r must be an error")
	}
}

func TestFingerprintMatchesDriveAlgorithm(t *testing.T) {
	// Drive: SHA-256 over the concatenated SHA-256 digests of each 8 MiB piece.
	reference := func(data []byte) string {
		var digests []byte
		for i := 0; i < len(data); i += PieceSize {
			end := min(i+PieceSize, len(data))
			sum := sha256.Sum256(data[i:end])
			digests = append(digests, sum[:]...)
		}
		total := sha256.Sum256(digests)
		return hex.EncodeToString(total[:])
	}
	for name, data := range map[string][]byte{
		"empty": {}, "small": []byte("hello"), "multi": make([]byte, 2*PieceSize+3),
	} {
		path := filepath.Join(t.TempDir(), name)
		if err := os.WriteFile(path, data, 0o644); err != nil {
			t.Fatal(err)
		}
		got, err := Fingerprint(path)
		if err != nil || got != reference(data) {
			t.Errorf("%s: got %s, %v", name, got, err)
		}
	}
	// Cross-checked against the server's Python implementation for "hello".
	path := filepath.Join(t.TempDir(), "hello")
	os.WriteFile(path, []byte("hello"), 0o644)
	if got, _ := Fingerprint(path); got != "9595c9df90075148eb06860365df33584b75bff782a510c6cd4883a419833d50" {
		t.Errorf("hello fingerprint %s", got)
	}
}

func TestResumeStore(t *testing.T) {
	store := Store{Dir: t.TempDir()}
	key := ResumeKey("https://drive.example.com", "Ugreen Drive", "Documents/a.mov", "/tmp/a.mov", 100, 5)
	if other := ResumeKey("https://drive.example.com", "Ugreen Drive", "Documents/a.mov", "/tmp/a.mov", 101, 5); other == key {
		t.Fatal("size change must change the key")
	}
	if _, ok := store.Load(key); ok {
		t.Fatal("empty store returned state")
	}
	if err := store.Save(key, State{UploadID: "u1", ChunkSize: 32, TotalChunks: 4}); err != nil {
		t.Fatal(err)
	}
	st, ok := store.Load(key)
	if !ok || st.UploadID != "u1" || st.TotalChunks != 4 {
		t.Fatalf("got %+v %v", st, ok)
	}
	info, _ := os.Stat(filepath.Join(store.Dir, key+".json"))
	// Windows has no Unix permission bits; %AppData% is private to the user.
	if runtime.GOOS != "windows" && info.Mode().Perm() != 0o600 {
		t.Fatalf("mode %v", info.Mode().Perm())
	}
	store.Delete(key)
	if _, ok := store.Load(key); ok {
		t.Fatal("deleted state still loads")
	}

	store.Save(key, State{UploadID: "old", SavedAt: time.Now().Add(-25 * time.Hour)})
	store.Prune(24 * time.Hour)
	if _, ok := store.Load(key); ok {
		t.Fatal("stale state survived prune")
	}
}
