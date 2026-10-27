package auth

import (
	"context"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"net/url"
	"strings"
	"testing"
	"time"
)

// fakeDrive approves (or denies) whatever the CLI opens, like a user clicking.
func fakeDrive(t *testing.T, allow bool, tamperState bool) (*httptest.Server, *string) {
	t.Helper()
	var challenge string
	mux := http.NewServeMux()
	mux.HandleFunc("/api/cli/token", func(w http.ResponseWriter, r *http.Request) {
		var body map[string]string
		json.NewDecoder(r.Body).Decode(&body)
		if body["code"] != "code-123" || Challenge(body["verifier"]) != challenge {
			w.WriteHeader(http.StatusUnauthorized)
			io.WriteString(w, `{"detail":"Sign-in code is invalid or expired."}`)
			return
		}
		io.WriteString(w, `{"token":"ugd_abc","id":"t1","username":"alex","device":"Test Mac"}`)
	})
	srv := httptest.NewServer(mux)
	t.Cleanup(srv.Close)
	return srv, &challenge
}

func browser(t *testing.T, allow, tamperState bool, challenge *string) func(string) error {
	return func(raw string) error {
		u, err := url.Parse(raw)
		if err != nil || u.Path != "/cli/authorize" {
			t.Errorf("unexpected authorize URL %q", raw)
		}
		q := u.Query()
		*challenge = q.Get("challenge")
		if q.Get("device") != "Test Mac" || len(q.Get("state")) < 16 {
			t.Errorf("missing device/state in %q", raw)
		}
		state := q.Get("state")
		if tamperState {
			state = "forged-state-value-000000"
		}
		cb := url.Values{"state": {state}}
		if allow {
			cb.Set("code", "code-123")
		} else {
			cb.Set("error", "access_denied")
		}
		go func() {
			res, err := http.Get("http://127.0.0.1:" + q.Get("port") + "/callback?" + cb.Encode())
			if err == nil {
				res.Body.Close()
			}
		}()
		return nil
	}
}

func run(t *testing.T, allow, tamper bool) (Result, error) {
	srv, challenge := fakeDrive(t, allow, tamper)
	server, _ := url.Parse(srv.URL)
	ctx, cancel := context.WithTimeout(context.Background(), 1500*time.Millisecond)
	defer cancel()
	return Login(ctx, Options{
		Server: server, Device: "Test Mac", Notify: io.Discard,
		OpenBrowser: browser(t, allow, tamper, challenge),
	})
}

func TestLoginApproved(t *testing.T) {
	result, err := run(t, true, false)
	if err != nil {
		t.Fatal(err)
	}
	if result.Token != "ugd_abc" || result.Username != "alex" {
		t.Fatalf("got %+v", result)
	}
}

func TestLoginDenied(t *testing.T) {
	_, err := run(t, false, false)
	if err == nil || !strings.Contains(err.Error(), "denied") {
		t.Fatalf("want denied error, got %v", err)
	}
}

func TestLoginIgnoresForgedState(t *testing.T) {
	_, err := run(t, true, true)
	if err == nil || !strings.Contains(err.Error(), "timed out") {
		t.Fatalf("forged state must not complete login, got %v", err)
	}
}

func TestChallengeIsS256(t *testing.T) {
	// Same S256 value the Drive computes (cli_tokens._pkce_challenge).
	if got := Challenge("dBjftJeZ4CVP-mB92K27uhbUJU1p1r7wXhFBU0Qs5T4"); got != "hOnutKLcWmbKxKsMVovP4Zk7m2mq_we7dsunbmRwfG8" {
		t.Fatalf("Challenge = %s", got)
	}
}

func TestConfirmationCodeMatchesConsentPage(t *testing.T) {
	// Expected value computed independently (Python) with the consent page's algorithm.
	if got := ConfirmationCode("s" + strings.Repeat("0", 31)); got != "2603-F3C6" {
		t.Fatalf("ConfirmationCode = %s", got)
	}
}
