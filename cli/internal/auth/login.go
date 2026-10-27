// Package auth runs `ugd login`: the browser approves this terminal and
// hands a one-time code to a listener on 127.0.0.1, which we trade (with the
// PKCE verifier only this process knows) for a Drive token.
package auth

import (
	"bytes"
	"context"
	"crypto/rand"
	"crypto/sha256"
	"crypto/subtle"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"html"
	"io"
	"net"
	"net/http"
	"net/url"
	"strings"
	"time"
)

// Timeout is how long login waits for the browser.
const Timeout = 5 * time.Minute

// Result is a freshly issued token.
type Result struct {
	Token    string `json:"token"`
	ID       string `json:"id"`
	Username string `json:"username"`
	Device   string `json:"device"`
}

// Options wires login to the outside world (swapped out in tests).
type Options struct {
	Server      *url.URL
	Device      string
	OpenBrowser func(string) error
	Notify      io.Writer
	HTTP        *http.Client
}

type callback struct {
	code string
	err  error
}

func randomString() (string, error) {
	buf := make([]byte, 32)
	if _, err := rand.Read(buf); err != nil {
		return "", err
	}
	return base64.RawURLEncoding.EncodeToString(buf), nil
}

// ConfirmationCode is shown by both the terminal and the consent page so the
// user can tell their own login from a link someone else sent them.
func ConfirmationCode(state string) string {
	sum := sha256.Sum256([]byte("ugd-verify:" + state))
	code := strings.ToUpper(hex.EncodeToString(sum[:4]))
	return code[:4] + "-" + code[4:]
}

// Challenge is the PKCE S256 challenge for a verifier.
func Challenge(verifier string) string {
	sum := sha256.Sum256([]byte(verifier))
	return base64.RawURLEncoding.EncodeToString(sum[:])
}

// Login runs the whole flow and returns the new token.
func Login(ctx context.Context, opts Options) (Result, error) {
	state, err := randomString()
	if err != nil {
		return Result{}, err
	}
	verifier, err := randomString()
	if err != nil {
		return Result{}, err
	}
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		return Result{}, fmt.Errorf("start local sign-in listener: %w", err)
	}
	results := make(chan callback, 1)
	server := &http.Server{Handler: callbackHandler(state, results), ReadHeaderTimeout: 10 * time.Second}
	go server.Serve(listener) //nolint:errcheck // Serve returns once we Shutdown below.
	defer func() {
		shutdownCtx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
		defer cancel()
		server.Shutdown(shutdownCtx) //nolint:errcheck
	}()

	authorize := *opts.Server
	authorize.Path = "/cli/authorize"
	authorize.RawQuery = url.Values{
		"port":      {fmt.Sprint(listener.Addr().(*net.TCPAddr).Port)},
		"state":     {state},
		"challenge": {Challenge(verifier)},
		"device":    {opts.Device},
	}.Encode()
	fmt.Fprintf(opts.Notify, "Confirmation code: %s  (the browser page must show the same code)\n", ConfirmationCode(state))
	if opts.OpenBrowser == nil {
		fmt.Fprintf(opts.Notify, "Open this link in your browser to approve this terminal:\n  %s\n", authorize.String())
	} else {
		fmt.Fprintf(opts.Notify, "Opening your browser to approve this terminal…\nIf it doesn't open, visit:\n  %s\n", authorize.String())
		if err := opts.OpenBrowser(authorize.String()); err != nil {
			fmt.Fprintf(opts.Notify, "(Couldn't open the browser automatically: %v)\n", err)
		}
	}

	ctx, cancel := context.WithTimeout(ctx, Timeout)
	defer cancel()
	var got callback
	select {
	case got = <-results:
	case <-ctx.Done():
		return Result{}, errors.New("timed out waiting for approval in the browser; run ugd login again")
	}
	if got.err != nil {
		return Result{}, got.err
	}
	return exchange(ctx, opts, got.code, verifier)
}

func callbackHandler(state string, results chan<- callback) http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("/callback", func(w http.ResponseWriter, r *http.Request) {
		q := r.URL.Query()
		if subtle.ConstantTimeCompare([]byte(q.Get("state")), []byte(state)) != 1 {
			// Not our browser round-trip (stale tab or a forged request): ignore it.
			writePage(w, http.StatusBadRequest, "This sign-in link doesn't match your terminal. Run ugd login again.")
			return
		}
		var result callback
		switch {
		case q.Get("error") == "access_denied":
			result.err = errors.New("access was denied in the browser")
			writePage(w, http.StatusOK, "Denied. The terminal was not signed in; you can close this tab.")
		case q.Get("code") == "":
			result.err = errors.New("the browser did not return a sign-in code")
			writePage(w, http.StatusBadRequest, "Sign-in failed. Run ugd login again.")
		default:
			result.code = q.Get("code")
			writePage(w, http.StatusOK, "Terminal signed in. You can close this tab.")
		}
		select {
		case results <- result:
		default: // already have a result; later hits are ignored
		}
	})
	return mux
}

func writePage(w http.ResponseWriter, status int, message string) {
	w.Header().Set("Content-Type", "text/html; charset=utf-8")
	w.Header().Set("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'")
	w.Header().Set("Referrer-Policy", "no-referrer")
	w.WriteHeader(status)
	fmt.Fprintf(w, `<!doctype html><meta charset="utf-8"><title>Ugreen Drive CLI</title>`+
		`<body style="font:16px system-ui;margin:15vh auto;max-width:420px;text-align:center">`+
		`<h1 style="font-size:20px">Ugreen Drive</h1><p>%s</p></body>`, html.EscapeString(message))
}

func exchange(ctx context.Context, opts Options, code, verifier string) (Result, error) {
	body, _ := json.Marshal(map[string]string{"code": code, "verifier": verifier})
	endpoint := *opts.Server
	endpoint.Path = "/api/cli/token"
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, endpoint.String(), bytes.NewReader(body))
	if err != nil {
		return Result{}, err
	}
	req.Header.Set("Content-Type", "application/json")
	client := opts.HTTP
	if client == nil {
		client = http.DefaultClient
	}
	res, err := client.Do(req)
	if err != nil {
		return Result{}, fmt.Errorf("finish sign-in: %w", err)
	}
	defer res.Body.Close()
	if res.StatusCode != http.StatusOK {
		var detail struct {
			Detail string `json:"detail"`
		}
		_ = json.NewDecoder(io.LimitReader(res.Body, 64<<10)).Decode(&detail)
		if detail.Detail == "" {
			detail.Detail = fmt.Sprintf("HTTP %d", res.StatusCode)
		}
		return Result{}, fmt.Errorf("sign-in failed: %s", detail.Detail)
	}
	var result Result
	if err := json.NewDecoder(res.Body).Decode(&result); err != nil || result.Token == "" {
		return Result{}, errors.New("sign-in failed: unexpected response from the Drive")
	}
	return result, nil
}
