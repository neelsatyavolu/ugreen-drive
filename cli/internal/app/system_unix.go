//go:build !windows

package app

import (
	"os"
	"os/exec"
	"strings"
)

func openBrowser(url string) error {
	return exec.Command("/usr/bin/open", url).Run()
}

// runEditor opens path in $VISUAL / $EDITOR (which may include args, e.g.
// "code --wait"), falling back to nano.
func runEditor(path string) error {
	editor := strings.TrimSpace(os.Getenv("VISUAL"))
	if editor == "" {
		editor = strings.TrimSpace(os.Getenv("EDITOR"))
	}
	if editor == "" {
		editor = "nano"
	}
	cmd := exec.Command("/bin/sh", "-c", editor+` "$1"`, "sh", path)
	cmd.Stdin, cmd.Stdout, cmd.Stderr = os.Stdin, os.Stdout, os.Stderr
	return cmd.Run()
}

func deviceName() string {
	if out, err := exec.Command("/usr/sbin/scutil", "--get", "ComputerName").Output(); err == nil {
		if name := strings.TrimSpace(string(out)); name != "" {
			return name
		}
	}
	if name, err := os.Hostname(); err == nil {
		return name
	}
	return "Mac"
}
