package app

import (
	"context"
	"errors"
	"flag"
	"fmt"
	"io"
	"net/url"

	"github.com/neelsatyavolu/ugreen-drive/cli/internal/auth"
	"github.com/neelsatyavolu/ugreen-drive/cli/internal/config"
)

func cmdLogin(ctx context.Context, r *runner, args []string) error {
	fs := flag.NewFlagSet("login", flag.ContinueOnError)
	device := fs.String("device", "", "name shown in Terminal sign-ins")
	server := fs.String("server", "", "Drive URL")
	noBrowser := fs.Bool("no-browser", false, "print the approval URL instead of opening it")
	rest, err := parseFlags(fs, args)
	if err != nil {
		return err
	}
	if len(rest) > 0 {
		return usagef("usage: ugd login [--server URL] [--device NAME] [--no-browser]")
	}
	if *server != "" {
		r.g.server = *server
	}
	serverURL, err := config.ParseServer(r.serverURLString())
	if err != nil {
		return usageError{err.Error()}
	}
	name := *device
	if name == "" {
		name = r.env.DeviceName()
	}
	open := r.env.OpenBrowser
	if *noBrowser {
		open = nil
	}
	notify := r.env.Stderr
	result, err := auth.Login(ctx, auth.Options{
		Server: serverURL, Device: name, OpenBrowser: open,
		Notify: notify, HTTP: r.env.HTTP,
	})
	if err != nil {
		return exitError{ExitAuth, err.Error()}
	}
	if err := r.env.Tokens.Set(serverURL.Host, result.Token); err != nil {
		return err
	}
	if r.cfg.Server != serverURL.String() {
		r.cfg.Server = serverURL.String()
		if err := config.Save(r.env.ConfigDir, r.cfg); err != nil {
			return err
		}
	}
	out := map[string]string{"username": result.Username, "device": result.Device, "server": serverURL.String()}
	return r.emit(out, func(w io.Writer) {
		fmt.Fprintf(w, "Signed in to %s as %s.\n", serverURL.Host, result.Username)
	})
}

func cmdLogout(ctx context.Context, r *runner, args []string) error {
	if len(args) > 0 {
		return usagef("usage: ugd logout")
	}
	client, err := r.signedIn()
	if errors.Is(err, config.ErrNoToken) {
		return r.emit(map[string]bool{"signed_out": true}, func(w io.Writer) {
			fmt.Fprintln(w, "Already signed out.")
		})
	}
	if err != nil {
		return err
	}
	var warning string
	if err := client.Logout(ctx); err != nil {
		// Still forget the local token; the server one expires on its own.
		warning = err.Error()
	}
	if err := r.env.Tokens.Delete(client.Base.Host); err != nil {
		return err
	}
	out := map[string]any{"signed_out": true}
	if warning != "" {
		out["warning"] = warning
	}
	return r.emit(out, func(w io.Writer) {
		fmt.Fprintf(w, "Signed out of %s.\n", client.Base.Host)
		if warning != "" {
			fmt.Fprintf(w, "(The Drive didn't confirm: %s)\n", warning)
		}
	})
}

func cmdWhoami(ctx context.Context, r *runner, args []string) error {
	if len(args) > 0 {
		return usagef("usage: ugd whoami")
	}
	client, err := r.signedIn()
	if err != nil {
		return err
	}
	me, err := client.Me(ctx)
	if err != nil {
		return err
	}
	if !me.Authenticated {
		return exitError{ExitAuth, "sign-in expired or revoked; run ugd login"}
	}
	return r.emit(me, func(w io.Writer) {
		who := me.Username
		if me.Email != "" {
			who += " (" + me.Email + ")"
		}
		fmt.Fprintf(w, "%s\nshare: %s\nserver: %s\n", who, me.Share, client.Base.Host)
	})
}

func cmdShares(ctx context.Context, r *runner, args []string) error {
	if len(args) > 0 {
		return usagef("usage: ugd shares")
	}
	client, err := r.signedIn()
	if err != nil {
		return err
	}
	me, err := client.Me(ctx)
	if err != nil {
		return err
	}
	out := map[string]any{"active": me.Share, "shares": me.Shares}
	return r.emit(out, func(w io.Writer) {
		for _, s := range me.Shares {
			mark := "  "
			if s.ID == me.Share {
				mark = "* "
			}
			line := mark + s.ID
			if s.Name != "" && s.Name != s.ID {
				line += "  (" + s.Name + ")"
			}
			if !s.CanWrite {
				line += "  [read-only]"
			}
			fmt.Fprintln(w, line)
		}
	})
}

func cmdShare(ctx context.Context, r *runner, args []string) error {
	if len(args) != 2 || args[0] != "use" {
		return usagef("usage: ugd share use NAME")
	}
	client, err := r.signedIn()
	if err != nil {
		return err
	}
	me, err := client.Me(ctx)
	if err != nil {
		return err
	}
	share, ok := me.FindShare(args[1])
	if !ok {
		return exitError{ExitNotFound, fmt.Sprintf("no share named %q (see ugd shares)", args[1])}
	}
	r.cfg.Share = share.ID
	if err := config.Save(r.env.ConfigDir, r.cfg); err != nil {
		return err
	}
	return r.emit(map[string]string{"share": share.ID, "name": share.Name}, func(w io.Writer) {
		fmt.Fprintf(w, "Now using share %q.\n", share.ID)
	})
}

func cmdVersion(_ context.Context, r *runner, _ []string) error {
	return r.emit(map[string]string{"version": Version, "commit": Commit}, func(w io.Writer) {
		fmt.Fprintf(w, "ugd %s (%s)\n", Version, Commit)
	})
}

func cmdHelp(_ context.Context, r *runner, args []string) error {
	if len(args) == 1 && args[0] == "agents" {
		fmt.Fprint(r.env.Stdout, agentGuide)
		return nil
	}
	if len(args) == 1 {
		if cmd, ok := commands[args[0]]; ok {
			fmt.Fprintf(r.env.Stdout, "usage: ugd %s\n  %s\n", cmd.usage, cmd.summary)
			return nil
		}
	}
	printUsage(r.env.Stdout)
	return nil
}

const agentGuide = `Ugreen Drive CLI — guide for AI agents

The human has already run "ugd login"; you act with their Drive permissions.
Every command accepts --json: results go to stdout as JSON, errors to stderr as
{"error": "...", "exit_code": N, "status": HTTP}. Nothing prompts when stdin is
not a terminal.

Paths are relative to the share root: "Documents/Taxes/2026.pdf".
"ugd --json shares" lists the shares you can open (id, name, can_write).
Use --share ID (or "ugd share use ID") for another one; personal folders
have ids like "~username".

Read:
  ugd --json ls "Documents"                  folder listing (items[].mtime_ns, size, is_dir)
  ugd --json tree "Documents" --depth 2      recursive listing
  ugd --json search "invoice"                name search
  ugd cat "Documents/notes.md"               file contents to stdout

Write:
  ugd put local.txt "Documents/notes.md"           create (fails with exit 4 if it exists)
  ugd put --force local.txt "Documents/notes.md"   overwrite
  printf 'text' | ugd put - "Documents/notes.md"   from stdin
  ugd --json put a.jpg b.jpg "Photos/Trip/"        several files into a folder
  ugd --json put -r ./Scans "Documents/"           a whole folder (exit 4 if some existed)
  ugd --json sync --dry-run ./Scans "Documents/Scans"   plan: upload/unchanged/conflict/drive_only
  ugd --json sync ./Scans "Documents/Scans"        upload new/changed files; never deletes
  Interrupted large uploads resume when you re-run the same command.
  ugd mkdir -p "Documents/New/Assets"
  ugd mv "Documents/a.pdf" "Archive"               move into a folder
  ugd rename "Documents/a.pdf" "b.pdf"
  ugd rm "Documents/old.pdf"                       moves to the Recycle bin

Safe edit without clobbering someone else's change:
  1. ugd --json ls "Documents" → note the file's mtime_ns
  2. ugd cat "Documents/notes.md" > notes.md ; edit locally
  3. ugd put --expect-mtime-ns <mtime_ns> notes.md "Documents/notes.md"
     exit 4 means the file changed on the Drive meanwhile: re-read and retry.

Exit codes: 0 ok · 1 error · 2 bad usage · 3 not signed in (ask the human to run
"ugd login") · 4 conflict / already exists · 5 not found or no permission
(also a locked personal folder: ask the human to run "ugd unlock") · 6 the NAS
wants the folder owner's sign-in first ("ugd unlock --nas-sign-in").
`

// cmdMoveServer follows the Drive to a new address: the saved sign-in moves to the new
// host and becomes the default server. The token itself is the same (the Drive issued it).
func cmdMoveServer(ctx context.Context, r *runner, args []string) error {
	if len(args) != 1 {
		return usagef("usage: ugd move-server NEW_URL")
	}
	from, err := config.ParseServer(r.serverURLString())
	if err != nil {
		return usageError{err.Error()}
	}
	to, err := config.ParseServer(args[0])
	if err != nil {
		return usageError{err.Error()}
	}
	moved, err := r.moveServer(from, to)
	if err != nil {
		return err
	}
	out := map[string]any{"server": to.String(), "moved_sign_in": moved}
	return r.emit(out, func(w io.Writer) {
		if moved {
			fmt.Fprintf(w, "Moved your sign-in from %s to %s.\n", from.Host, to.Host)
		} else {
			fmt.Fprintf(w, "Now using %s.\n", to.Host)
		}
	})
}

// moveServer moves the saved sign-in from one Drive address to another and saves the new one
// as the default server. Reports whether there was a sign-in to move.
func (r *runner) moveServer(from, to *url.URL) (bool, error) {
	moved := false
	if from.Host != to.Host {
		token, err := r.env.Tokens.Get(from.Host)
		switch {
		case errors.Is(err, config.ErrNoToken):
		case err != nil:
			return false, err
		default:
			if err := r.env.Tokens.Set(to.Host, token); err != nil {
				return false, err
			}
			if err := r.env.Tokens.Delete(from.Host); err != nil {
				return false, err
			}
			moved = true
		}
	}
	if r.cfg.Server != to.String() {
		r.cfg.Server = to.String()
		if err := config.Save(r.env.ConfigDir, r.cfg); err != nil {
			return false, err
		}
	}
	return moved, nil
}
