//go:build windows

package app

import (
	"errors"
	"os"
	"os/exec"
	"strings"
)

func openBrowser(url string) error {
	// url.dll opens the default browser without going through a shell.
	return exec.Command("rundll32", "url.dll,FileProtocolHandler", url).Run()
}

// runEditor opens path in %VISUAL% / %EDITOR% (which may include args, e.g.
// "code --wait"), falling back to Notepad.
func runEditor(path string) error {
	editor := strings.TrimSpace(os.Getenv("VISUAL"))
	if editor == "" {
		editor = strings.TrimSpace(os.Getenv("EDITOR"))
	}
	if editor == "" {
		editor = "notepad"
	}
	fields := strings.Fields(editor)
	if len(fields) == 0 {
		return errors.New("no editor configured")
	}
	cmd := exec.Command(fields[0], append(fields[1:], path)...)
	cmd.Stdin, cmd.Stdout, cmd.Stderr = os.Stdin, os.Stdout, os.Stderr
	return cmd.Run()
}

func deviceName() string {
	if name, err := os.Hostname(); err == nil && name != "" {
		return name
	}
	return "Windows PC"
}
