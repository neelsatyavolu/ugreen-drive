package api

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"sync/atomic"
	"time"
)

// Route is another way to reach the same Drive: on the home network, its
// LAN address (no tunnel) with a short-lived LAN-only token.
type Route struct {
	Base    *url.URL
	Token   string
	Expires time.Time
}

// LANRoute holds the current LAN route, shared by every copy of a Client.
// While set, requests go to the LAN; any network error or 401 there drops it
// and the request is retried over the internet when it can be.
type LANRoute struct {
	p        atomic.Pointer[Route]
	OnChange func(viaLAN bool) // optional
}

// Get returns the active route, or nil when using the internet.
func (l *LANRoute) Get() *Route {
	if l == nil {
		return nil
	}
	return l.p.Load()
}

// Set switches to r.
func (l *LANRoute) Set(r *Route) {
	if old := l.p.Swap(r); old == nil && l.OnChange != nil {
		l.OnChange(true)
	}
}

// Drop switches back to the internet.
func (l *LANRoute) Drop() {
	if old := l.p.Swap(nil); old != nil && l.OnChange != nil {
		l.OnChange(false)
	}
}

func (l *LANRoute) carried(req *http.Request) bool {
	r := l.Get()
	return r != nil && req.URL.Host == r.Base.Host && req.URL.Scheme == r.Base.Scheme
}

// Internet returns a copy of the client that never uses the LAN route
// (minting LAN tokens, for one, must go over the verified HTTPS route).
func (c *Client) Internet() *Client {
	cp := *c
	cp.LAN = nil
	return &cp
}

// retryOverInternet rebuilds req for the internet route, or nil if its body
// can't be replayed (the caller's own retry then goes over the internet).
func (c *Client) retryOverInternet(req *http.Request) *http.Request {
	if req.Body != nil && req.GetBody == nil {
		return nil
	}
	retry := req.Clone(req.Context())
	retry.URL.Scheme, retry.URL.Host, retry.Host = c.Base.Scheme, c.Base.Host, ""
	retry.Header.Set("Authorization", "Bearer "+c.Token)
	if req.GetBody != nil {
		body, err := req.GetBody()
		if err != nil {
			return nil
		}
		retry.Body = body
	}
	return retry
}

// LANGrant is a short-lived LAN-only token.
type LANGrant struct {
	Token     string `json:"token"`
	LANOrigin string `json:"lan_origin"`
	ExpiresIn int64  `json:"expires_in"`
}

// LANProof asks a LAN host to sign nonce with the Drive's secret (no token
// is sent: we don't know yet whether that host is really the Drive).
func (c *Client) LANProof(ctx context.Context, lan *url.URL, nonce string) (string, error) {
	u := *lan
	u.Path = "/api/lan/proof"
	u.RawQuery = url.Values{"nonce": {nonce}}.Encode()
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, u.String(), nil)
	if err != nil {
		return "", err
	}
	res, err := c.httpClient().Do(req)
	if err != nil {
		return "", err
	}
	defer res.Body.Close()
	var out struct {
		Proof string `json:"proof"`
	}
	if res.StatusCode != http.StatusOK || json.NewDecoder(io.LimitReader(res.Body, 4096)).Decode(&out) != nil {
		return "", fmt.Errorf("LAN host answered HTTP %d", res.StatusCode)
	}
	return out.Proof, nil
}

// LANToken trades a LAN proof for a LAN token, over the internet (HTTPS).
func (c *Client) LANToken(ctx context.Context, nonce, proof string) (LANGrant, error) {
	var grant LANGrant
	err := c.Internet().postForm(ctx, "/api/cli/lan-token", url.Values{"nonce": {nonce}, "proof": {proof}}, &grant)
	return grant, err
}

// Healthy reports whether the Drive answers at base right now.
func (c *Client) Healthy(ctx context.Context, base *url.URL) bool {
	u := *base
	u.Path = "/api/health"
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, u.String(), nil)
	if err != nil {
		return false
	}
	res, err := c.httpClient().Do(req)
	if err != nil {
		return false
	}
	res.Body.Close()
	return res.StatusCode == http.StatusOK
}
