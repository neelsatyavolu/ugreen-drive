package davfs

import (
	"crypto/sha256"
	"crypto/subtle"
	"mime"
	"net"
	"net/http"
	"os"
	"path"
	"sync"
	"time"

	"golang.org/x/net/webdav"
)

// User is the fixed WebDAV user name; the per-run password is the secret.
const User = "ugd"

// Handler serves fs over WebDAV under prefix (e.g. "/Ugreen Drive").
//
// It listens on loopback only, but other local accounts and web pages can
// still reach loopback, so every request needs the password (HTTP Basic),
// and Host must be loopback to block DNS-rebinding pages.
func Handler(fs *FS, prefix, password string, logf func(format string, args ...any)) http.Handler {
	dav := &webdav.Handler{
		Prefix:     prefix,
		FileSystem: WithSpeedTest(fs),
		LockSystem: fs.lockSystem(),
		Logger: func(r *http.Request, err error) {
			if err != nil && !os.IsNotExist(err) && logf != nil {
				logf("%s %s: %v", r.Method, r.URL.Path, err)
			}
		},
	}
	want := sha256.Sum256([]byte(User + ":" + password))
	var h http.Handler = http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if !loopbackHost(r.Host) {
			http.Error(w, "forbidden", http.StatusForbidden)
			return
		}
		user, pass, _ := r.BasicAuth()
		got := sha256.Sum256([]byte(user + ":" + pass))
		if subtle.ConstantTimeCompare(got[:], want[:]) != 1 {
			w.Header().Set("WWW-Authenticate", `Basic realm="Ugreen Drive"`)
			http.Error(w, "unauthorized", http.StatusUnauthorized)
			return
		}
		if r.Method == http.MethodGet || r.Method == http.MethodHead {
			// Otherwise http.ServeContent reads the first 512 bytes to sniff
			// the type: a Drive read of its own, for every extensionless file.
			ctype := mime.TypeByExtension(path.Ext(r.URL.Path))
			if ctype == "" {
				ctype = "application/octet-stream"
			}
			w.Header().Set("Content-Type", ctype)
		}
		dav.ServeHTTP(w, withRequest(r, prefix))
	})
	if os.Getenv("UGD_DAV_TRACE") != "" && logf != nil {
		h = trace(h, logf)
	}
	return h
}

// trace logs every request with its status and duration (UGD_DAV_TRACE=1),
// to see what Finder asks for and where the time goes.
func trace(next http.Handler, logf func(format string, args ...any)) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		start := time.Now()
		rec := &statusRecorder{ResponseWriter: w, status: http.StatusOK}
		next.ServeHTTP(rec, r)
		logf("trace %s %s %s depth=%q range=%q len=%d -> %d %dB %s", start.Format("05.000"), r.Method, r.URL.Path,
			r.Header.Get("Depth"), r.Header.Get("Range"), r.ContentLength, rec.status, rec.bytes,
			time.Since(start).Round(100*time.Microsecond))
	})
}

type statusRecorder struct {
	http.ResponseWriter
	status int
	bytes  int64
}

func (s *statusRecorder) WriteHeader(code int) {
	s.status = code
	s.ResponseWriter.WriteHeader(code)
}

func (s *statusRecorder) Write(p []byte) (int, error) {
	n, err := s.ResponseWriter.Write(p)
	s.bytes += int64(n)
	return n, err
}

func loopbackHost(hostport string) bool {
	host, _, err := net.SplitHostPort(hostport)
	if err != nil {
		host = hostport
	}
	if host == "localhost" {
		return true
	}
	ip := net.ParseIP(host)
	return ip != nil && ip.IsLoopback()
}

// pendingLocks remembers which name each lock is for, so an UNLOCK (or the
// lock expiring, or shutdown) can create a file Finder LOCKed but never wrote
// (see FS.finishPending; after an UNLOCK, FS.finishLater). x/net/webdav also takes a short lock around every
// write, so expired locks are swept on that activity.
type pendingLocks struct {
	webdav.LockSystem
	fs    *FS
	mu    sync.Mutex
	roots map[string]lockInfo // lock token → what it locks
}

type lockInfo struct {
	root    string
	expires time.Time // zero: no timeout
}

func (l *pendingLocks) Create(now time.Time, details webdav.LockDetails) (string, error) {
	l.sweep(now)
	token, err := l.LockSystem.Create(now, details)
	if err == nil {
		l.mu.Lock()
		l.roots[token] = lockInfo{root: details.Root, expires: expiry(now, details.Duration)}
		l.mu.Unlock()
	}
	return token, err
}

func (l *pendingLocks) Refresh(now time.Time, token string, duration time.Duration) (webdav.LockDetails, error) {
	details, err := l.LockSystem.Refresh(now, token, duration)
	if err == nil {
		l.mu.Lock()
		if info, ok := l.roots[token]; ok {
			info.expires = expiry(now, duration)
			l.roots[token] = info
		}
		l.mu.Unlock()
	}
	return details, err
}

func (l *pendingLocks) Unlock(now time.Time, token string) error {
	if err := l.LockSystem.Unlock(now, token); err != nil {
		return err // still held (e.g. by a PUT): nothing is finished yet
	}
	l.mu.Lock()
	info, ok := l.roots[token]
	delete(l.roots, token)
	l.mu.Unlock()
	if ok {
		// Not right away: macOS unlocks a new file once before it writes it.
		l.fs.finishLaterName(info.root)
	}
	l.sweep(now)
	return nil
}

// sweep finishes placeholders whose locks expired without an UNLOCK.
func (l *pendingLocks) sweep(now time.Time) {
	var expired []string
	l.mu.Lock()
	for token, info := range l.roots {
		if !info.expires.IsZero() && now.After(info.expires) {
			expired = append(expired, info.root)
			delete(l.roots, token)
		}
	}
	l.mu.Unlock()
	for _, root := range expired {
		l.fs.finishPending(root)
	}
}

func (l *pendingLocks) finishAll() {
	l.mu.Lock()
	roots := make([]string, 0, len(l.roots))
	for token, info := range l.roots {
		roots = append(roots, info.root)
		delete(l.roots, token)
	}
	l.mu.Unlock()
	for _, root := range roots {
		l.fs.finishPending(root)
	}
}

func expiry(now time.Time, d time.Duration) time.Time {
	if d < 0 {
		return time.Time{} // infinite
	}
	return now.Add(d)
}

func (f *FS) lockSystem() webdav.LockSystem {
	f.locks = &pendingLocks{LockSystem: webdav.NewMemLS(), fs: f, roots: map[string]lockInfo{}}
	return f.locks
}
