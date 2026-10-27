// Package app implements the `ugd` commands.
package app

import (
	"context"
	"crypto/tls"
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
	"github.com/neelsatyavolu/ugreen-drive/cli/internal/config"
)

// Exit codes (documented in `ugd help agents`).
const (
	ExitOK       = 0
	ExitError    = 1
	ExitUsage    = 2
	ExitAuth     = 3
	ExitConflict = 4
	ExitNotFound = 5
	// ExitNASSignIn: UGOS wants `ugd unlock --nas-sign-in` before unlocking.
	ExitNASSignIn = 6
)

// Build info, set with -ldflags "-X …/app.Version=… -X …/app.Commit=…".
var (
	Version = "dev"
	Commit  = "none"
)

// Env is everything a command touches outside the process (swapped in tests).
type Env struct {
	Stdin       io.Reader
	Stdout      io.Writer
	Stderr      io.Writer
	StdinIsTTY  bool
	ConfigDir   string
	Tokens      config.TokenStore
	HTTP        *http.Client
	Bulk        *http.Client // large transfers (optional; default HTTP)
	OpenBrowser func(string) error
	RunEditor   func(path string) error
	DeviceName  func() string
	Getenv      func(string) string
	StderrIsTTY bool                                // progress lines and update notices only on a terminal
	Now         func() time.Time                    // nil = time.Now
	ReadSecret  func(prompt string) (string, error) // no-echo terminal prompt
}

// globals are flags accepted before or after the command name.
type globals struct {
	json   bool
	share  string
	server string
	help   bool
}

type cmdFunc func(ctx context.Context, r *runner, args []string) error

type command struct {
	run     cmdFunc
	usage   string
	summary string
}

var commands map[string]command

func init() {
	commands = map[string]command{
		"login":       {cmdLogin, "login [--server URL] [--device NAME] [--no-browser]", "Sign this terminal in (opens your browser)"},
		"logout":      {cmdLogout, "logout", "Sign this terminal out and revoke its token"},
		"whoami":      {cmdWhoami, "whoami", "Show the signed-in account and active share"},
		"move-server": {cmdMoveServer, "move-server NEW_URL", "Follow the Drive to a new address, keeping this sign-in"},
		"shares":      {cmdShares, "shares", "List the shares you can use"},
		"share":       {cmdShare, "share use NAME", "Set the default share for later commands"},
		"ls":          {cmdLs, "ls [PATH]", "List a folder"},
		"tree":        {cmdTree, "tree [PATH] [--depth N]", "List a folder recursively (default depth 3)"},
		"search":      {cmdSearch, "search QUERY [--path P] [--limit N]", "Search file and folder names"},
		"cat":         {cmdCat, "cat PATH", "Print a file to stdout"},
		"get":         {cmdGet, "get PATH [LOCAL|-] [--force]", "Download a file (folders download as .zip)"},
		"put":         {cmdPut, "put [-r] [--force] SRC... DEST", "Upload files, folders (-r) or stdin (-); resumes if interrupted"},
		"sync":        {cmdSync, "sync LOCAL_DIR REMOTE_DIR [--dry-run]", "Upload new/changed files from a folder; never deletes"},
		"edit":        {cmdEdit, "edit PATH", "Edit a file in $EDITOR and save it back safely"},
		"mkdir":       {cmdMkdir, "mkdir PATH [-p]", "Create a folder (-p: parents too, ok if it exists)"},
		"mv":          {cmdMv, "mv SRC... DEST_FOLDER", "Move items into a folder"},
		"rename":      {cmdRename, "rename PATH NEW_NAME", "Rename an item in place"},
		"rm":          {cmdRm, "rm PATH... [-y]", "Move items to the Recycle bin"},
		"unlock":      {cmdUnlock, "unlock [--key-file PATH] | unlock --nas-sign-in", "Unlock your encrypted personal folder for 24 hours"},
		"webdav":      {cmdWebdav, "webdav [--addr 127.0.0.1:PORT] [--name NAME]", "Serve your shares to Finder over local WebDAV (used by the Mac app)"},
		"version":     {cmdVersion, "version", "Print the CLI version"},
		"help":        {cmdHelp, "help [agents]", "Show help (help agents: guide for AI agents)"},
	}
}

var commandOrder = []string{
	"login", "logout", "whoami", "move-server", "shares", "share", "unlock",
	"ls", "tree", "search", "cat", "get", "put", "sync", "edit",
	"mkdir", "mv", "rename", "rm", "webdav", "version", "help",
}

// runner is one invocation's state.
type runner struct {
	env    Env
	g      globals
	cfg    config.Config
	client *api.Client // set by signedIn()
}

// usageError is a bad invocation (exit 2).
type usageError struct{ msg string }

func (e usageError) Error() string { return e.msg }

func usagef(format string, args ...any) error { return usageError{fmt.Sprintf(format, args...)} }

// Run executes argv (without the program name) and returns the exit code.
func Run(ctx context.Context, argv []string, env Env) int {
	g, rest, err := splitGlobals(argv)
	r := &runner{env: env, g: g}
	if err != nil {
		return r.fail(err)
	}
	if len(rest) == 0 {
		printUsage(env.Stdout)
		return ExitOK
	}
	name, args := rest[0], rest[1:]
	cmd, ok := commands[name]
	if !ok {
		return r.fail(usagef("unknown command %q (see ugd help)", name))
	}
	if g.help {
		fmt.Fprintf(env.Stdout, "usage: ugd %s\n  %s\n", cmd.usage, cmd.summary)
		return ExitOK
	}
	cfg, err := config.Load(env.ConfigDir)
	if err != nil {
		return r.fail(err)
	}
	r.cfg = cfg
	return r.fail(cmd.run(ctx, r, args))
}

func (r *runner) now() time.Time {
	if r.env.Now != nil {
		return r.env.Now()
	}
	return time.Now()
}

func splitGlobals(argv []string) (globals, []string, error) {
	var g globals
	var rest []string
	for i := 0; i < len(argv); i++ {
		arg := argv[i]
		name, value, hasValue := strings.Cut(arg, "=")
		switch name {
		case "--json":
			g.json = true
		case "-h", "--help":
			g.help = true
		case "--share", "--server":
			if !hasValue {
				if i+1 >= len(argv) {
					return g, nil, usagef("%s needs a value", name)
				}
				i++
				value = argv[i]
			}
			if name == "--share" {
				g.share = value
			} else {
				g.server = value
			}
		case "--":
			return g, append(rest, argv[i+1:]...), nil
		default:
			rest = append(rest, arg)
		}
	}
	return g, rest, nil
}

// parseFlags parses flags that may appear anywhere among the positionals.
func parseFlags(fs *flag.FlagSet, args []string) ([]string, error) {
	fs.SetOutput(io.Discard)
	var positional []string
	for {
		if err := fs.Parse(args); err != nil {
			return nil, usagef("%v", err)
		}
		args = fs.Args()
		if len(args) == 0 {
			return positional, nil
		}
		if args[0] == "-" { // stdin marker, not a flag
			positional = append(positional, args[0])
			args = args[1:]
			continue
		}
		positional = append(positional, args[0])
		args = args[1:]
	}
}

// fail reports err (if any) and maps it to an exit code.
func (r *runner) fail(err error) int {
	if err == nil {
		return ExitOK
	}
	code, status := ExitError, 0
	var apiErr *api.Error
	var usage usageError
	var exit exitError
	switch {
	case errors.As(err, &usage):
		code = ExitUsage
	case errors.Is(err, config.ErrNoToken):
		code = ExitAuth
		err = errors.New("not signed in; run ugd login")
	case errors.As(err, &apiErr):
		status = apiErr.Status
		switch apiErr.Status {
		case http.StatusUnauthorized:
			code = ExitAuth
		case http.StatusConflict:
			code = ExitConflict
		case http.StatusForbidden, http.StatusNotFound:
			code = ExitNotFound
		case http.StatusLocked:
			code = ExitNotFound
			err = fmt.Errorf("%w (run: ugd unlock)", err)
		}
	case errors.As(err, &exit):
		code = exit.code
	}
	if r.g.json {
		out := map[string]any{"error": err.Error(), "exit_code": code}
		if status != 0 {
			out["status"] = status
		}
		json.NewEncoder(r.env.Stderr).Encode(out) //nolint:errcheck
	} else {
		fmt.Fprintf(r.env.Stderr, "ugd: %v\n", err)
	}
	return code
}

// exitError carries a specific exit code for non-API failures.
type exitError struct {
	code int
	msg  string
}

func (e exitError) Error() string { return e.msg }

func (r *runner) serverURLString() string {
	if r.g.server != "" {
		return r.g.server
	}
	if v := r.env.Getenv("UGD_SERVER"); v != "" {
		return v
	}
	return r.cfg.Server
}

// signedIn prepares an API client with the saved token.
func (r *runner) signedIn() (*api.Client, error) {
	server, err := config.ParseServer(r.serverURLString())
	if err != nil {
		return nil, usageError{err.Error()}
	}
	token, err := r.env.Tokens.Get(server.Host)
	if err != nil {
		return nil, err
	}
	share := r.g.share
	if share == "" {
		share = r.cfg.Share
	}
	r.client = &api.Client{
		Base: server, Token: token, Share: share, HTTP: r.env.HTTP, Bulk: r.env.Bulk,
		UserAgent: "ugd/" + Version,
	}
	return r.client, nil
}

// emit prints v as JSON in --json mode, otherwise calls human().
func (r *runner) emit(v any, human func(w io.Writer)) error {
	if r.g.json {
		enc := json.NewEncoder(r.env.Stdout)
		enc.SetIndent("", "  ")
		return enc.Encode(v)
	}
	human(r.env.Stdout)
	return nil
}

func printUsage(w io.Writer) {
	fmt.Fprintln(w, "ugd — Ugreen Drive from your terminal")
	fmt.Fprintln(w, "\nusage: ugd [--json] [--share NAME] [--server URL] <command> [args]")
	fmt.Fprintln(w, "\ncommands:")
	for _, name := range commandOrder {
		cmd := commands[name]
		fmt.Fprintf(w, "  %-38s %s\n", cmd.usage, cmd.summary)
	}
	fmt.Fprintln(w, "\nPaths are relative to the share root, e.g. \"Documents/Taxes/2026.pdf\".")
	fmt.Fprintln(w, "AI agents: run  ugd help agents")
}

// DefaultEnv is the real terminal environment.
func DefaultEnv() (Env, error) {
	dir, err := config.Dir()
	if err != nil {
		return Env{}, err
	}
	info, _ := os.Stdin.Stat()
	errInfo, _ := os.Stderr.Stat()
	return Env{
		StderrIsTTY: errInfo != nil && errInfo.Mode()&os.ModeCharDevice != 0,
		Stdin:       os.Stdin,
		Stdout:      os.Stdout,
		Stderr:      os.Stderr,
		StdinIsTTY:  info != nil && info.Mode()&os.ModeCharDevice != 0,
		ConfigDir:   dir,
		Tokens:      config.Keychain{},
		HTTP:        &http.Client{Transport: traceUpstream(fastTransport())},
		Bulk:        &http.Client{Transport: traceUpstream(bulkTransport())},
		OpenBrowser: openBrowser,
		ReadSecret:  readSecret,
		RunEditor:   runEditor,
		DeviceName:  deviceName,
		Getenv:      os.Getenv,
	}, nil
}

// fastTransport keeps enough idle connections for parallel chunk uploads and
// read-ahead (Go's default keeps 2 per host, so extra streams would redo the
// TLS handshake through the tunnel every time). Idle connections stay open
// for minutes, and HTTP/2 pings keep the tunnel connection alive between
// Finder's bursts and notice a dead one (after a network change) in seconds.
func fastTransport() *http.Transport {
	t := http.DefaultTransport.(*http.Transport).Clone()
	t.MaxIdleConnsPerHost = 16
	t.IdleConnTimeout = 5 * time.Minute
	t.ReadBufferSize = 256 << 10
	t.WriteBufferSize = 256 << 10
	t.HTTP2 = &http.HTTP2Config{SendPingTimeout: 15 * time.Second, PingTimeout: 10 * time.Second}
	return t
}

// bulkTransport carries large transfers (upload bodies, downloads) on their
// own HTTP/1.1 connections, closed after 30 s idle. Each transfer then runs on
// fresh TCP connections with their own congestion state: sharing the one
// long-lived HTTP/2 connection, an app that had been up an hour (through a
// network change) uploaded at ~10 MB/s where fresh connections did ~45, since
// TCP that has seen losses ramps up slowly after every pause.
func bulkTransport() *http.Transport {
	t := http.DefaultTransport.(*http.Transport).Clone()
	t.ForceAttemptHTTP2 = false
	t.TLSClientConfig = &tls.Config{NextProtos: []string{"http/1.1"}}
	t.TLSNextProto = map[string]func(string, *tls.Conn) http.RoundTripper{}
	t.MaxIdleConnsPerHost = 16
	t.IdleConnTimeout = 30 * time.Second
	t.ReadBufferSize = 256 << 10
	t.WriteBufferSize = 256 << 10
	return t
}

// traceUpstream logs each Drive request's time to first byte when
// UGD_DAV_TRACE is set (see davfs.Handler).
func traceUpstream(t http.RoundTripper) http.RoundTripper {
	if os.Getenv("UGD_DAV_TRACE") == "" {
		return t
	}
	return roundTripFunc(func(req *http.Request) (*http.Response, error) {
		start := time.Now()
		res, err := t.RoundTrip(req)
		status := 0
		if res != nil {
			status = res.StatusCode
		}
		fmt.Fprintf(os.Stderr, "upstream %s %s %s %s range=%q -> %d err=%v %s\n", start.Format("05.000"), req.Method,
			req.URL.Host, req.URL.Path, req.Header.Get("Range"), status, err, time.Since(start).Round(100*time.Microsecond))
		return res, err
	})
}

type roundTripFunc func(*http.Request) (*http.Response, error)

func (f roundTripFunc) RoundTrip(req *http.Request) (*http.Response, error) { return f(req) }
