//go:build !windows

package config

import (
	"bytes"
	"errors"
	"fmt"
	"os/exec"
	"strings"
)

// Keychain stores tokens in the macOS login keychain via /usr/bin/security.
type Keychain struct{}

func (Keychain) Get(host string) (string, error) {
	if !safeHost.MatchString(host) {
		return "", fmt.Errorf("invalid server host %q", host)
	}
	out, err := exec.Command("/usr/bin/security", "find-generic-password",
		"-s", keychainService, "-a", host, "-w").Output()
	if err != nil {
		var exit *exec.ExitError
		if errors.As(err, &exit) && exit.ExitCode() == 44 { // item not found
			return "", ErrNoToken
		}
		return "", fmt.Errorf("read Keychain: %w", err)
	}
	return strings.TrimSpace(string(out)), nil
}

func (Keychain) Set(host, token string) error {
	if !safeHost.MatchString(host) || !safeToken.MatchString(token) {
		return errors.New("refusing to store malformed server host or token")
	}
	// `security -i` reads the command from stdin so the token never appears in
	// the process list (argv is visible to other local users).
	cmd := exec.Command("/usr/bin/security", "-i")
	cmd.Stdin = strings.NewReader(fmt.Sprintf(
		"add-generic-password -U -s %s -a %s -l %s -w %s\n",
		keychainService, host, `"Ugreen Drive CLI"`, token))
	var stderr bytes.Buffer
	cmd.Stderr = &stderr
	if err := cmd.Run(); err != nil {
		return fmt.Errorf("save to Keychain: %w: %s", err, strings.TrimSpace(stderr.String()))
	}
	return nil
}

func (Keychain) Delete(host string) error {
	if !safeHost.MatchString(host) {
		return fmt.Errorf("invalid server host %q", host)
	}
	err := exec.Command("/usr/bin/security", "delete-generic-password",
		"-s", keychainService, "-a", host).Run()
	var exit *exec.ExitError
	if err != nil && !(errors.As(err, &exit) && exit.ExitCode() == 44) {
		return fmt.Errorf("remove from Keychain: %w", err)
	}
	return nil
}
