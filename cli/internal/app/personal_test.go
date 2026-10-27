package app

import (
	"encoding/json"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/neelsatyavolu/ugreen-drive/cli/internal/davfs"
)

func lockedHarness(t *testing.T) *harness {
	t.Helper()
	h := newHarness(t)
	h.drive.personalLocked = true
	h.drive.personalKey = "open-sesame"
	return h
}

func TestUnlockWithPasswordFromPrompt(t *testing.T) {
	h := lockedHarness(t)
	h.env.StdinIsTTY = true
	var prompts []string
	h.env.ReadSecret = func(prompt string) (string, error) {
		prompts = append(prompts, prompt)
		return "open-sesame", nil
	}
	out := h.mustRun(t, "unlock")
	if h.drive.personalLocked || !strings.Contains(out, "Unlocked") {
		t.Fatalf("still locked; out=%q", out)
	}
	if len(prompts) != 1 || !strings.Contains(prompts[0], "Encryption password") {
		t.Fatalf("prompts = %q", prompts)
	}
}

func TestUnlockWithKeyFileAndFromStdin(t *testing.T) {
	h := lockedHarness(t)
	keyFile := filepath.Join(t.TempDir(), "key.txt")
	os.WriteFile(keyFile, []byte("open-sesame"), 0o600)
	var status struct {
		Locked bool   `json:"locked"`
		Share  string `json:"share"`
	}
	if err := json.Unmarshal([]byte(h.mustRun(t, "--json", "unlock", "--key-file", keyFile)), &status); err != nil {
		t.Fatal(err)
	}
	if status.Locked || status.Share != "~alex" {
		t.Fatalf("status = %+v", status)
	}

	h = lockedHarness(t)
	h.env.Stdin = strings.NewReader("open-sesame\n") // how the Mac app passes it: never argv
	h.mustRun(t, "--json", "unlock")
	if h.drive.personalLocked {
		t.Fatal("stdin key did not unlock")
	}
}

func TestUnlockWrongKeyAndAlreadyUnlocked(t *testing.T) {
	h := lockedHarness(t)
	h.env.Stdin = strings.NewReader("wrong\n")
	if code := h.run("unlock"); code != ExitError || !strings.Contains(h.stderr.String(), "encryption key") {
		t.Fatalf("exit %d: %s", code, h.stderr)
	}
	h.drive.personalLocked = false
	calls := h.drive.unlockCalls
	out := h.mustRun(t, "unlock")
	if !strings.Contains(out, "already unlocked") || h.drive.unlockCalls != calls {
		t.Fatalf("out=%q calls=%d", out, h.drive.unlockCalls-calls)
	}
}

func TestUnlockNeedsNASSignInThenOTP(t *testing.T) {
	h := lockedHarness(t)
	h.drive.needsOwner = true
	h.drive.ownerOTP = true
	h.env.Stdin = strings.NewReader("open-sesame")
	if code := h.run("--json", "unlock"); code != ExitNASSignIn {
		t.Fatalf("exit %d, want %d: %s", code, ExitNASSignIn, h.stderr)
	}

	// Machine mode (the Mac app): JSON on stdin, one step per run.
	h.env.Stdin = strings.NewReader(`{"password":"wrong"}`)
	if code := h.run("--json", "unlock", "--nas-sign-in"); code != ExitError {
		t.Fatalf("wrong NAS password: exit %d (must not look like a lost CLI sign-in)", code)
	}
	h.env.Stdin = strings.NewReader(`{"password":"nas-pass"}`)
	var step struct {
		NeedOTP bool   `json:"need_otp"`
		Pending string `json:"pending"`
	}
	json.Unmarshal([]byte(h.mustRun(t, "--json", "unlock", "--nas-sign-in")), &step)
	if !step.NeedOTP || step.Pending == "" {
		t.Fatalf("step = %+v", step)
	}
	h.env.Stdin = strings.NewReader(`{"pending":"` + step.Pending + `","code":"123456"}`)
	h.mustRun(t, "--json", "unlock", "--nas-sign-in")
	if h.drive.needsOwner {
		t.Fatal("owner sign-in not completed")
	}
	h.env.Stdin = strings.NewReader("open-sesame")
	h.mustRun(t, "unlock")
	if h.drive.personalLocked {
		t.Fatal("still locked after sign-in")
	}
}

func TestUnlockNASSignInInteractive(t *testing.T) {
	h := lockedHarness(t)
	h.drive.needsOwner = true
	h.drive.ownerOTP = true
	h.env.StdinIsTTY = true
	h.env.Stdin = strings.NewReader("123456\n")
	h.env.ReadSecret = func(string) (string, error) { return "nas-pass", nil }
	out := h.mustRun(t, "unlock", "--nas-sign-in")
	if h.drive.needsOwner || !strings.Contains(out, "ugd unlock") {
		t.Fatalf("out=%q needsOwner=%v", out, h.drive.needsOwner)
	}
}

func TestWebdavLockedPersonalFolder(t *testing.T) {
	h := newDavHarness(t)
	h.set(func(d *fakeDrive) { d.personalLocked = true; d.personalKey = "k" })
	h.drive.dir("Notes")
	// Still visible at the top of the volume, like on the website.
	if _, body := h.propfind(t, h.url()+"/"); !strings.Contains(body, "My%20folder") {
		t.Fatalf("locked folder missing from the volume root:\n%s", body)
	}
	// Inside: only a note saying how to unlock it.
	code, body := h.propfind(t, h.url("My folder")+"/")
	if code != http.StatusMultiStatus || strings.Contains(body, "Notes") || !strings.Contains(body, "unlock") {
		t.Fatalf("PROPFIND locked folder: %d\n%s", code, body)
	}
	if code, note := h.do(t, "GET", h.url("My folder", davfs.LockedNote), ""); code != 200 || !strings.Contains(note, "Unlock") {
		t.Fatalf("GET note: %d %q", code, note)
	}
	if code, _ := h.do(t, "PUT", h.url("My folder", "a.txt"), "x"); code/100 == 2 {
		t.Fatalf("PUT into locked folder succeeded: %d", code)
	}
	if code, _ := h.do(t, "GET", h.url("My folder", "Notes"), ""); code/100 == 2 {
		t.Fatalf("read inside locked folder succeeded: %d", code)
	}
	h.set(func(d *fakeDrive) { d.personalLocked = false }) // unlocked on the website
	time.Sleep(1100 * time.Millisecond)                    // davfs re-checks locked shares once a second
	if code, body := h.propfind(t, h.url("My folder")+"/"); code != http.StatusMultiStatus || !strings.Contains(body, "Notes") {
		t.Fatalf("after unlock: %d", code)
	}
	if code, _ := h.do(t, "PUT", h.url("My folder", "a.txt"), "x"); code != http.StatusCreated {
		t.Fatalf("PUT right after unlock: %d (stale read-only share cache?)", code)
	}
}

func TestUnlockNASSignInRecoversFromExpiredCode(t *testing.T) {
	h := lockedHarness(t)
	h.drive.needsOwner = true
	h.env.Stdin = strings.NewReader(`{"pending":"expired","code":"123456"}`)
	if code := h.run("--json", "unlock", "--nas-sign-in"); code != ExitNASSignIn {
		t.Fatalf("expired code step: exit %d, want %d (start over)", code, ExitNASSignIn)
	}
	h.env.Stdin = strings.NewReader(`{"pending":"pending-blob","code":"000000"}`)
	if code := h.run("--json", "unlock", "--nas-sign-in"); code != ExitError {
		t.Fatalf("wrong code: exit %d, want %d (try another code)", code, ExitError)
	}
	h.env.Stdin = strings.NewReader(`{}`)
	if code := h.run("--json", "unlock", "--nas-sign-in"); code != ExitUsage {
		t.Fatalf("empty input: exit %d", code)
	}
	h.env.Stdin = strings.NewReader(`{"password":"` + strings.Repeat("x", 300) + `"}`)
	if code := h.run("--json", "unlock", "--nas-sign-in"); code != ExitUsage || strings.Contains(h.stderr.String(), "xxxx") {
		t.Fatalf("long password: exit %d, stderr %q", code, h.stderr)
	}
}
