package app

import (
	"bufio"
	"context"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"net/http"
	"os"
	"strings"
	"time"

	"github.com/neelsatyavolu/ugreen-drive/cli/internal/api"
)

const maxKeyBytes = 64 << 10 // the Drive's limit for keys and key files

// cmdUnlock unlocks your UGOS-encrypted personal folder for 24 hours, like
// the web app. Secrets come from a no-echo prompt, a key file or stdin —
// never from argv, which other local users can see.
func cmdUnlock(ctx context.Context, r *runner, args []string) error {
	fs := flag.NewFlagSet("unlock", flag.ContinueOnError)
	keyFile := fs.String("key-file", "", "UGOS encryption key file")
	nasSignIn := fs.Bool("nas-sign-in", false, "sign in to the NAS as the folder owner (when unlock asks)")
	rest, err := parseFlags(fs, args)
	if err != nil {
		return err
	}
	if len(rest) > 0 {
		return usagef("usage: ugd unlock [--key-file PATH] | ugd unlock --nas-sign-in")
	}
	client, err := r.signedIn()
	if err != nil {
		return err
	}
	me, err := client.Me(ctx) // also proves the CLI sign-in is valid before any NAS 401
	if err != nil {
		return err
	}
	share, ok := me.FindShare("~" + me.Username)
	if !ok {
		return exitError{ExitNotFound, "your account has no personal folder on this Drive"}
	}
	if *nasSignIn {
		return r.nasSignIn(ctx, client, me.Username)
	}
	if !share.Encrypted && !share.Locked {
		return r.emit(map[string]any{"share": share.ID, "locked": false, "encrypted": false}, func(w io.Writer) {
			fmt.Fprintf(w, "%s isn't encrypted; there is nothing to unlock.\n", share.ID)
		})
	}
	if !share.Locked {
		return r.emit(map[string]any{"share": share.ID, "locked": false, "expires_at": share.ExpiresAt}, func(w io.Writer) {
			fmt.Fprintf(w, "%s is already unlocked%s.\n", share.ID, relockText(share.ExpiresAt))
		})
	}
	key, isFile, err := r.readKey(*keyFile)
	if err != nil {
		return err
	}
	status, err := client.UnlockPersonal(ctx, me.Username, key, isFile)
	var apiErr *api.Error
	if errors.As(err, &apiErr) && apiErr.Status == http.StatusPreconditionRequired {
		return exitError{ExitNASSignIn, apiErr.Error() + " Run: ugd unlock --nas-sign-in"}
	}
	if err != nil {
		return err
	}
	if status.Locked {
		return exitError{ExitError, "UGOS is still unlocking the folder; try again shortly"}
	}
	return r.emit(map[string]any{"share": share.ID, "locked": false, "expires_at": status.ExpiresAt}, func(w io.Writer) {
		fmt.Fprintf(w, "Unlocked %s%s.\n", share.ID, relockText(status.ExpiresAt))
	})
}

func relockText(expires *float64) string {
	if expires == nil {
		return ""
	}
	return " until " + time.Unix(int64(*expires), 0).Format("Mon 3:04 PM")
}

// readKey returns the encryption password or key-file contents.
func (r *runner) readKey(keyFile string) (string, bool, error) {
	if keyFile != "" {
		info, err := os.Stat(keyFile)
		if err != nil {
			return "", false, err
		}
		if info.Size() > maxKeyBytes {
			return "", false, usagef("key files must be under 64 KB")
		}
		raw, err := os.ReadFile(keyFile)
		return string(raw), true, err
	}
	var key string
	if r.env.StdinIsTTY && r.env.ReadSecret != nil {
		secret, err := r.env.ReadSecret("Encryption password: ")
		if err != nil {
			return "", false, err
		}
		key = secret
	} else {
		raw, err := io.ReadAll(io.LimitReader(r.env.Stdin, maxKeyBytes+1))
		if err != nil {
			return "", false, err
		}
		if len(raw) > maxKeyBytes {
			return "", false, usagef("the key must be under 64 KB")
		}
		key = strings.TrimRight(string(raw), "\r\n")
	}
	if key == "" {
		return "", false, usagef("enter the encryption password (or use --key-file)")
	}
	return key, false, nil
}

// checkNASInput rejects what the Drive would refuse anyway, before sending it
// (limits match /api/personal/auth).
func checkNASInput(password, code string) error {
	switch {
	case password == "" && code == "":
		return usagef("enter the NAS password (or the authenticator code)")
	case len(password) > 256:
		return usagef("that NAS password is too long")
	case len(code) > 12:
		return usagef("authenticator codes are at most 12 characters")
	}
	return nil
}

// nasSignIn approves unlocking as the folder owner. Interactive: prompts for
// the NAS password and, if asked, the authenticator code. Otherwise (the Mac
// app) one step per run from JSON on stdin: {"password":…} then
// {"pending":…,"code":…}; the output says {"need_otp":true,"pending":…} or
// {"ok":true}.
func (r *runner) nasSignIn(ctx context.Context, client *api.Client, owner string) error {
	var in struct {
		Password string `json:"password"`
		Code     string `json:"code"`
		Pending  string `json:"pending"`
	}
	interactive := r.env.StdinIsTTY && r.env.ReadSecret != nil
	if interactive {
		password, err := r.env.ReadSecret(fmt.Sprintf("NAS password for %s: ", owner))
		if err != nil {
			return err
		}
		in.Password = password
	} else if err := json.NewDecoder(io.LimitReader(r.env.Stdin, 16<<10)).Decode(&in); err != nil {
		return usagef(`ugd unlock --nas-sign-in reads {"password":…} or {"pending":…,"code":…} on stdin`)
	}
	in.Code = strings.TrimSpace(in.Code)
	if err := checkNASInput(in.Password, in.Code); err != nil {
		return err
	}
	auth, err := client.AuthPersonal(ctx, owner, in.Password, in.Code, in.Pending)
	if interactive && err == nil && auth.NeedOTP {
		fmt.Fprint(r.env.Stderr, "Authenticator code: ")
		line, readErr := bufio.NewReader(r.env.Stdin).ReadString('\n')
		code := strings.TrimSpace(line)
		if readErr != nil && code == "" {
			return usagef("no authenticator code entered")
		}
		if err := checkNASInput("", code); err != nil {
			return err
		}
		auth, err = client.AuthPersonal(ctx, owner, "", code, auth.Pending)
	}
	var apiErr *api.Error
	if errors.As(err, &apiErr) {
		switch {
		case apiErr.Status == http.StatusGone:
			// The code step expired or was used up: start over with the password.
			return exitError{ExitNASSignIn, apiErr.Error()}
		case apiErr.Status == http.StatusUnauthorized && strings.Contains(apiErr.Detail, "ugd login"):
			return err // the CLI sign-in itself was revoked: exit 3
		case apiErr.Status == http.StatusUnauthorized:
			return exitError{ExitError, apiErr.Error()} // wrong NAS password or code
		}
	}
	if err != nil {
		return err
	}
	return r.emit(auth, func(w io.Writer) {
		fmt.Fprintf(w, "Signed in to the NAS as %s. Now run: ugd unlock\n", owner)
	})
}
