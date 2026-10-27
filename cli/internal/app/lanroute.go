package app

import (
	"context"
	"crypto/rand"
	"encoding/base64"
	"net/url"
	"time"

	"github.com/neelsatyavolu/ugreen-drive/cli/internal/api"
)

const (
	lanCheckEvery = 30 * time.Second
	lanProbeLimit = 3 * time.Second
	lanRenewAt    = 30 * time.Minute // renew the LAN token this long before it expires
	lanOriginTTL  = 15 * time.Minute // re-read the Drive's LAN address this often
)

// lanMonitor puts the helper on the Drive's LAN address while it's reachable
// (on the home network) and keeps it there. It never sends a token to a
// LAN host before that host proved it is this Drive, and it only ever sends
// the short-lived LAN token there. Leaving the network is handled by the
// client itself (any LAN failure falls back to the internet at once); this
// re-checks every 30 s and whenever the app says the network changed.
type lanMonitor struct {
	client *api.Client // its LAN route is what this manages
	// origin is the Drive's LAN address from /api/me (as of originAt), so
	// checks off the home network don't ask /api/me every time.
	origin   string
	originAt time.Time
	// onLatency gets the round trip of each check's ping on the route in
	// use: what every Finder request waits for (optional).
	onLatency func(ms int, viaLAN bool)
}

func (m *lanMonitor) run(ctx context.Context, probe <-chan struct{}) {
	m.check(ctx)
	ticker := time.NewTicker(lanCheckEvery)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
		case <-probe:
		}
		m.check(ctx)
	}
}

func (m *lanMonitor) check(ctx context.Context) {
	route := m.client.LAN
	if r := route.Get(); r != nil {
		if !m.ping(ctx, r.Base, true) {
			route.Drop()
			return
		}
		if time.Until(r.Expires) > lanRenewAt {
			return
		}
	}
	if r, ok := m.connect(ctx); ok {
		route.Set(r)
		m.ping(ctx, r.Base, true)
		return
	}
	if route.Get() == nil {
		m.ping(ctx, m.client.Base, false)
	}
}

// ping checks base answers (within lanProbeLimit) and reports how long it took.
func (m *lanMonitor) ping(ctx context.Context, base *url.URL, viaLAN bool) bool {
	probeCtx, cancel := context.WithTimeout(ctx, lanProbeLimit)
	defer cancel()
	start := time.Now()
	ok := m.client.Healthy(probeCtx, base)
	if ok && m.onLatency != nil {
		m.onLatency(int(time.Since(start).Milliseconds()), viaLAN)
	}
	return ok
}

// lanOrigin returns the Drive's LAN address, asking /api/me when the one it
// has is older than lanOriginTTL.
func (m *lanMonitor) lanOrigin(ctx context.Context) string {
	if m.originAt.IsZero() || time.Since(m.originAt) > lanOriginTTL {
		me, err := m.client.Internet().Me(ctx)
		if err != nil {
			return m.origin
		}
		m.origin, m.originAt = me.LANOrigin, time.Now()
	}
	return m.origin
}

// connect finds the Drive's LAN address, checks it's reachable and genuinely
// this Drive, and gets a LAN token for it.
func (m *lanMonitor) connect(ctx context.Context) (*api.Route, bool) {
	internet := m.client.Internet()
	origin := m.lanOrigin(ctx)
	if origin == "" {
		return nil, false
	}
	lan, err := url.Parse(origin)
	if err != nil || (lan.Scheme != "http" && lan.Scheme != "https") || lan.Host == "" {
		return nil, false
	}
	probeCtx, cancel := context.WithTimeout(ctx, lanProbeLimit)
	defer cancel()
	if !m.client.Healthy(probeCtx, lan) {
		return nil, false // not on the home network
	}
	raw := make([]byte, 32)
	if _, err := rand.Read(raw); err != nil {
		return nil, false
	}
	nonce := base64.RawURLEncoding.EncodeToString(raw)
	proof, err := m.client.LANProof(probeCtx, lan, nonce)
	if err != nil {
		return nil, false
	}
	grant, err := internet.LANToken(ctx, nonce, proof) // the Drive checks the proof over HTTPS
	if err != nil || grant.LANOrigin != origin {
		m.originAt = time.Time{} // the address may have changed: re-read it next time
		return nil, false
	}
	return &api.Route{Base: lan, Token: grant.Token, Expires: time.Now().Add(time.Duration(grant.ExpiresIn) * time.Second)}, true
}
