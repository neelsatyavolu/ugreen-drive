package app

import (
	"bytes"
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func localTree(t *testing.T, files map[string]string) string {
	t.Helper()
	root := t.TempDir()
	for rel, data := range files {
		p := filepath.Join(root, rel)
		if err := os.MkdirAll(filepath.Dir(p), 0o755); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(p, []byte(data), 0o644); err != nil {
			t.Fatal(err)
		}
	}
	return root
}

func (h *harness) data(t *testing.T, p string) string {
	t.Helper()
	f := h.drive.files[p]
	if f == nil || f.isDir {
		t.Fatalf("%s missing on the fake Drive", p)
	}
	return string(f.data)
}

func TestPutManyFilesIntoNewFolder(t *testing.T) {
	h := newHarness(t)
	root := localTree(t, map[string]string{"a.txt": "a", "b.txt": "bb"})
	h.mustRun(t, "put", filepath.Join(root, "a.txt"), filepath.Join(root, "b.txt"), "Batch")
	if h.data(t, "Batch/a.txt") != "a" || h.data(t, "Batch/b.txt") != "bb" {
		t.Fatal("contents differ")
	}
}

func TestPutRecursiveFolderSkipsJunk(t *testing.T) {
	h := newHarness(t)
	h.drive.dir("Documents")
	root := localTree(t, map[string]string{
		"Footage/a.mov": "a", "Footage/Day 2/b.mov": "b", "Footage/.DS_Store": "junk", "Footage/._a.mov": "junk",
	})
	os.MkdirAll(filepath.Join(root, "Footage", "Empty"), 0o755)
	h.mustRun(t, "put", "-r", filepath.Join(root, "Footage"), "Documents/")
	if h.data(t, "Documents/Footage/a.mov") != "a" || h.data(t, "Documents/Footage/Day 2/b.mov") != "b" {
		t.Fatal("folder contents differ")
	}
	for _, junk := range []string{"Documents/Footage/.DS_Store", "Documents/Footage/._a.mov"} {
		if _, ok := h.drive.files[junk]; ok {
			t.Fatalf("uploaded junk %s", junk)
		}
	}
	if f := h.drive.files["Documents/Footage/Empty"]; f == nil || !f.isDir {
		t.Fatal("empty subfolder not created")
	}
}

func TestPutFolderNeedsRecursive(t *testing.T) {
	h := newHarness(t)
	root := localTree(t, map[string]string{"Footage/a.mov": "a"})
	if code := h.run("put", filepath.Join(root, "Footage"), "Documents/"); code != ExitUsage {
		t.Fatalf("exit %d, stderr %s", code, h.stderr)
	}
}

func TestPutBatchContinuesPastExistingFiles(t *testing.T) {
	h := newHarness(t)
	h.drive.dir("Batch")
	h.drive.put("Batch/a.txt", "theirs")
	root := localTree(t, map[string]string{"a.txt": "mine", "b.txt": "b"})
	code := h.run("--json", "put", filepath.Join(root, "a.txt"), filepath.Join(root, "b.txt"), "Batch")
	if code != ExitConflict {
		t.Fatalf("exit %d, stderr %s", code, h.stderr)
	}
	var summary struct {
		Uploaded []string `json:"uploaded"`
		Exists   []string `json:"exists"`
	}
	if err := json.Unmarshal(h.stdout.Bytes(), &summary); err != nil {
		t.Fatalf("summary %q: %v", h.stdout, err)
	}
	if len(summary.Uploaded) != 1 || summary.Uploaded[0] != "Batch/b.txt" || len(summary.Exists) != 1 {
		t.Fatalf("summary %+v", summary)
	}
	if h.data(t, "Batch/a.txt") != "theirs" || h.data(t, "Batch/b.txt") != "b" {
		t.Fatal("wrong contents")
	}
	h.mustRun(t, "put", "--force", filepath.Join(root, "a.txt"), filepath.Join(root, "b.txt"), "Batch")
	if h.data(t, "Batch/a.txt") != "mine" {
		t.Fatal("--force did not overwrite")
	}
}

func TestPutResumesInterruptedUpload(t *testing.T) {
	h := newHarness(t)
	big := bytes.Repeat([]byte("r"), 70<<20) // 4 chunks of 18 MiB
	root := t.TempDir()
	local := filepath.Join(root, "big.mov")
	os.WriteFile(local, big, 0o644)

	h.drive.failAlways[2] = true // the network dies on the last piece
	if code := h.run("put", local, "big.mov"); code != ExitError {
		t.Fatalf("interrupted upload: exit %d", code)
	}
	h.drive.mu.Lock()
	aborts := h.drive.aborts
	h.drive.mu.Unlock()
	if aborts != 0 {
		t.Fatal("an interrupted upload must not be aborted on the Drive")
	}
	if states, _ := filepath.Glob(filepath.Join(h.env.ConfigDir, "uploads", "*.json")); len(states) != 1 {
		t.Fatalf("resume state files: %v", states)
	}

	h.drive.mu.Lock()
	delete(h.drive.failAlways, 2)
	h.drive.chunkPuts = 0
	h.drive.mu.Unlock()
	h.mustRun(t, "put", local, "big.mov")
	h.drive.mu.Lock()
	defer h.drive.mu.Unlock()
	if h.drive.chunkPuts != 1 {
		t.Fatalf("resume re-sent %d pieces, want 1", h.drive.chunkPuts)
	}
	if !bytes.Equal(h.drive.files["big.mov"].data, big) {
		t.Fatal("resumed upload content mismatch")
	}
	if states, _ := filepath.Glob(filepath.Join(h.env.ConfigDir, "uploads", "*.json")); len(states) != 0 {
		t.Fatalf("resume state left behind: %v", states)
	}
}

func TestSyncUploadsOnlyNewAndChangedAndNeverDeletes(t *testing.T) {
	h := newHarness(t)
	h.drive.dir("Proj")
	h.drive.put("Proj/same.txt", "same")
	h.drive.put("Proj/changed.txt", "old")
	h.drive.put("Proj/samesize.txt", "AAAA")
	h.drive.put("Proj/driveonly.txt", "keep me")
	root := localTree(t, map[string]string{
		"new.txt": "new", "same.txt": "same", "changed.txt": "newer", "samesize.txt": "BBBB", "Sub/deep.txt": "d",
	})

	h.mustRun(t, "--json", "sync", "--dry-run", root, "Proj")
	var plan struct {
		Upload    []string `json:"upload"`
		Unchanged []string `json:"unchanged"`
		DriveOnly []string `json:"drive_only"`
	}
	if err := json.Unmarshal(h.stdout.Bytes(), &plan); err != nil {
		t.Fatalf("plan %q: %v", h.stdout, err)
	}
	if strings.Join(plan.Upload, ",") != "Proj/Sub/deep.txt,Proj/changed.txt,Proj/new.txt,Proj/samesize.txt" ||
		strings.Join(plan.Unchanged, ",") != "Proj/same.txt" || strings.Join(plan.DriveOnly, ",") != "Proj/driveonly.txt" {
		t.Fatalf("plan %+v", plan)
	}
	if _, ok := h.drive.files["Proj/new.txt"]; ok {
		t.Fatal("dry run uploaded something")
	}

	h.mustRun(t, "sync", root, "Proj")
	for p, want := range map[string]string{"Proj/new.txt": "new", "Proj/changed.txt": "newer",
		"Proj/samesize.txt": "BBBB", "Proj/Sub/deep.txt": "d", "Proj/driveonly.txt": "keep me"} {
		if got := h.data(t, p); got != want {
			t.Fatalf("%s = %q, want %q", p, got, want)
		}
	}

	h.mustRun(t, "--json", "sync", root, "Proj")
	var second struct {
		Uploaded []string `json:"uploaded"`
	}
	json.Unmarshal(h.stdout.Bytes(), &second)
	if len(second.Uploaded) != 0 {
		t.Fatalf("second sync uploaded %v", second.Uploaded)
	}
}

func TestSyncReportsFileFolderClashAsConflict(t *testing.T) {
	h := newHarness(t)
	h.drive.dir("Proj")
	h.drive.dir("Proj/clip")
	root := localTree(t, map[string]string{"clip": "a file here", "ok.txt": "ok"})
	if code := h.run("sync", root, "Proj"); code != ExitConflict {
		t.Fatalf("exit %d, stderr %s", code, h.stderr)
	}
	if h.data(t, "Proj/ok.txt") != "ok" {
		t.Fatal("other files should still sync")
	}
}

func TestPutBatchSkipsExistingFilesWithoutSendingThem(t *testing.T) {
	h := newHarness(t)
	h.drive.dir("Batch")
	h.drive.put("Batch/big.mov", "already here")
	root := t.TempDir()
	os.WriteFile(filepath.Join(root, "big.mov"), bytes.Repeat([]byte("x"), 40<<20), 0o644)
	os.WriteFile(filepath.Join(root, "new.txt"), []byte("n"), 0o644)
	if code := h.run("put", filepath.Join(root, "big.mov"), filepath.Join(root, "new.txt"), "Batch"); code != ExitConflict {
		t.Fatalf("exit %d, stderr %s", code, h.stderr)
	}
	if h.drive.chunkPuts != 0 {
		t.Fatalf("sent %d pieces of a file that already exists", h.drive.chunkPuts)
	}
	if h.data(t, "Batch/new.txt") != "n" {
		t.Fatal("new file not uploaded")
	}
}
