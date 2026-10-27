package app

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"path"
	"sort"
	"strconv"
	"strings"
	"sync"
	"testing"
	"time"
)

const testToken = "ugd_test"

type fakeFile struct {
	data    []byte
	mtimeNS int64
	isDir   bool
}

// fakeDrive is an in-memory stand-in for the Drive API the CLI uses.
type fakeDrive struct {
	t          *testing.T
	mu         sync.Mutex
	files      map[string]*fakeFile
	clock      int64
	chunks     map[string]map[int][]byte
	meta       map[string][3]string // upload_id → dir, name, total chunks
	failOnce   map[int]bool         // chunk index → return 502 once
	failAlways map[int]bool         // chunk index → always 502 (interrupted upload)
	chunkPuts  int
	aborts     int
	shares     []map[string]any
	lastShare  string
	loggedOut  bool
	revoked    bool           // token rejected with 401
	rangeReads int            // downloads that asked for a byte range
	failLists  int            // next N folder listings fail with 502
	listCalls  int            // /api/files requests
	onRange    func()         // runs after each ranged download (tests swap files mid-read)
	failDelete bool           // /api/delete answers 403
	speedUp    int64          // bytes received by /api/speedtest/upload
	speedPuts  int            // /api/speedtest/upload requests
	lanURL     string         // set by withLAN: the fake's "LAN address"
	lanHits    int            // requests that arrived over the fake LAN
	uploads    int            // finished uploads (simple or chunked)
	failUpload bool           // uploads fail with 507 (e.g. quota)
	truncate   map[string]int // path → download is cut off after N bytes
	rangeBytes int64          // bytes asked for by ranged downloads
	meDelay    time.Duration  // /api/me answers this late (UGOS can be slow)
	upDelay    time.Duration  // each chunk of a chunked upload takes this long

	// The personal folder ~alex: UGOS encryption.
	personalLocked bool
	needsOwner     bool   // UGOS wants the owner's NAS sign-in first (428)
	personalKey    string // encryption password that unlocks it
	ownerOTP       bool   // NAS sign-in asks for an authenticator code
	unlockCalls    int
}

func newFakeDrive(t *testing.T) (*fakeDrive, *httptest.Server) {
	d := &fakeDrive{
		t: t, files: map[string]*fakeFile{"": {isDir: true}}, clock: 1_000,
		chunks: map[string]map[int][]byte{}, meta: map[string][3]string{},
		failOnce: map[int]bool{}, failAlways: map[int]bool{},
		// Same shape as shares.list_shares_for_user on the real Drive.
		shares: []map[string]any{
			{"id": "Ugreen Drive", "name": "Ugreen Drive", "kind": "shared", "can_read": true, "can_write": true},
			{"id": "Photos", "name": "Photos", "kind": "shared", "can_read": true, "can_write": false},
			{"id": "~alex", "name": "My folder", "kind": "personal", "can_read": true, "can_write": true},
		},
	}
	srv := httptest.NewServer(http.HandlerFunc(d.serve))
	t.Cleanup(srv.Close)
	return d, srv
}

func (d *fakeDrive) put(p string, data string) {
	d.clock++
	d.files[p] = &fakeFile{data: []byte(data), mtimeNS: d.clock}
}

func (d *fakeDrive) dir(p string) { d.files[p] = &fakeFile{isDir: true} }

func (d *fakeDrive) entry(p string) map[string]any {
	f := d.files[p]
	return map[string]any{"name": path.Base(p), "path": p, "is_dir": f.isDir,
		"size": len(f.data), "mtime": "2026-09-24T10:00:00+00:00", "mtime_ns": f.mtimeNS}
}

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	json.NewEncoder(w).Encode(v)
}

func fail(w http.ResponseWriter, status int, detail string) {
	writeJSON(w, status, map[string]string{"detail": detail})
}

func join(dir, name string) string {
	if dir == "" {
		return name
	}
	return dir + "/" + name
}

func (d *fakeDrive) serve(w http.ResponseWriter, r *http.Request) {
	if r.URL.Path == "/api/me" || r.URL.Path == "/api/upload/chunk" {
		d.mu.Lock()
		delay := d.meDelay
		if r.URL.Path != "/api/me" {
			delay = d.upDelay
		}
		d.mu.Unlock()
		time.Sleep(delay) // without the lock: only this request is slow
	}
	d.mu.Lock()
	defer d.mu.Unlock()
	viaLAN := r.Header.Get("X-Test-Lan") != ""
	if viaLAN {
		d.lanHits++
	}
	switch r.URL.Path {
	case "/api/health":
		writeJSON(w, 200, map[string]any{"status": "ok"})
		return
	case "/api/lan/proof": // like the real Drive: only answers direct LAN requests
		if !viaLAN {
			fail(w, 404, "Not Found")
			return
		}
		writeJSON(w, 200, map[string]any{"proof": "p-" + r.URL.Query().Get("nonce")})
		return
	}
	auth := r.Header.Get("Authorization")
	lanToken := auth == "Bearer ugl_lan" && viaLAN
	if d.revoked || (auth != "Bearer "+testToken && !lanToken) {
		fail(w, 401, "Terminal sign-in expired or revoked. Run `ugd login`.")
		return
	}
	if r.URL.Path == "/api/cli/lan-token" {
		if r.FormValue("proof") != "p-"+r.FormValue("nonce") || lanToken {
			fail(w, 403, "That LAN host isn't this Drive")
			return
		}
		writeJSON(w, 200, map[string]any{"token": "ugl_lan", "lan_origin": d.lanURL, "expires_in": 28800})
		return
	}
	d.lastShare = r.Header.Get("X-Drive-Share")
	q := r.URL.Query()
	if d.lastShare == "~alex" && d.personalLocked && strings.HasPrefix(r.URL.Path, "/api/files") {
		fail(w, 423, "Personal folder is locked. Enter its encryption key to unlock it.")
		return
	}
	switch r.URL.Path {
	case "/api/me":
		shares := append([]map[string]any(nil), d.shares...)
		for i, s := range shares {
			if s["id"] == "~alex" && !d.personalLocked && d.personalKey != "" {
				unlocked := map[string]any{"encrypted": true, "locked": false, "expires_at": 1_900_000_000}
				for k, v := range s {
					unlocked[k] = v
				}
				shares[i] = unlocked
			}
			if s["id"] == "~alex" && d.personalLocked {
				locked := map[string]any{"encrypted": true, "locked": true, "can_read": false, "can_write": false}
				if d.needsOwner {
					locked["needs_owner_signin"] = true
				}
				for k, v := range s {
					if _, set := locked[k]; !set {
						locked[k] = v
					}
				}
				shares[i] = locked
			}
		}
		writeJSON(w, 200, map[string]any{"authenticated": true, "nas_username": "alex",
			"email": "alex@example.org", "share": "Ugreen Drive", "shares": shares, "lan_origin": d.lanURL})
	case "/api/speedtest/download":
		size, _ := strconv.Atoi(q.Get("size"))
		if r.Header.Get("Range") != "" {
			d.rangeReads++
		}
		data := make([]byte, size)
		for i := range data {
			data[i] = byte(i % 251)
		}
		w.Header().Set("ETag", `"speedtest"`)
		http.ServeContent(w, r, "", time.Time{}, bytes.NewReader(data))
	case "/api/speedtest/upload":
		n, _ := io.Copy(io.Discard, r.Body)
		d.speedUp += n
		d.speedPuts++
		writeJSON(w, 200, map[string]any{"received": n})
	case "/api/personal/unlock":
		d.unlockCalls++
		switch {
		case r.FormValue("owner") != "alex":
			fail(w, 403, "You cannot unlock this personal folder")
		case d.needsOwner:
			fail(w, 428, "This folder is private. Sign in as its NAS owner to unlock it and enable automatic relocking.")
		case r.FormValue("key") != d.personalKey:
			fail(w, 400, "Could not unlock the folder. Check the encryption key and try again.")
		default:
			d.personalLocked = false
			writeJSON(w, 200, map[string]any{"encrypted": true, "locked": false, "expires_at": 1_900_000_000})
		}
	case "/api/personal/auth":
		switch {
		case r.FormValue("code") != "":
			if r.FormValue("pending") != "pending-blob" {
				fail(w, 410, "Sign-in expired. Enter your NAS password again.")
				return
			}
			if r.FormValue("code") != "123456" {
				fail(w, 401, "Wrong code")
				return
			}
			d.needsOwner = false
			writeJSON(w, 200, map[string]any{"ok": true})
		case r.FormValue("password") != "nas-pass":
			fail(w, 401, "Incorrect NAS password")
		case d.ownerOTP:
			writeJSON(w, 200, map[string]any{"need_otp": true, "pending": "pending-blob"})
		default:
			d.needsOwner = false
			writeJSON(w, 200, map[string]any{"ok": true})
		}
	case "/api/files":
		if d.failLists > 0 {
			d.failLists--
			fail(w, 502, "bad gateway")
			return
		}
		d.listCalls++
		dir := q.Get("path")
		if f, ok := d.files[dir]; !ok || !f.isDir {
			fail(w, 404, "Not found")
			return
		}
		items := []map[string]any{}
		var names []string
		for p := range d.files {
			if p != "" && path.Dir("/"+p) == path.Clean("/"+dir) {
				names = append(names, p)
			}
		}
		sort.Strings(names)
		for _, p := range names {
			items = append(items, d.entry(p))
		}
		writeJSON(w, 200, map[string]any{"path": dir, "items": items})
	case "/api/download":
		f, ok := d.files[q.Get("path")]
		if !ok || f.isDir {
			fail(w, 404, "Not found")
			return
		}
		if rng := r.Header.Get("Range"); rng != "" {
			d.rangeReads++
			d.rangeBytes += requestedBytes(rng, int64(len(f.data)))
			if d.onRange != nil {
				defer d.onRange()
			}
		}
		w.Header().Set("ETag", fmt.Sprintf(`"%d"`, f.mtimeNS)) // like FileResponse: changes when the file does
		if n, ok := d.truncate[q.Get("path")]; ok {
			w.Header().Set("Content-Length", strconv.Itoa(len(f.data)))
			w.Write(f.data[:n])
			panic(http.ErrAbortHandler) // drop the connection mid-body
		}
		http.ServeContent(w, r, "", time.Time{}, bytes.NewReader(f.data))
	case "/api/upload", "/api/upload/complete":
		if d.failUpload {
			fail(w, 507, "Drive is full")
			return
		}
		if r.URL.Path == "/api/upload/complete" {
			d.completeUpload(w, r)
			return
		}
		r.ParseMultipartForm(64 << 20)
		file, header, err := r.FormFile("file")
		if err != nil {
			fail(w, 422, "missing file")
			return
		}
		data, _ := io.ReadAll(file)
		d.finishUpload(w, join(r.FormValue("path"), header.Filename), data, r.FormValue("expect_mtime_ns"))
	case "/api/upload/init":
		id := fmt.Sprintf("u%d", len(d.meta)+1)
		d.chunks[id] = map[int][]byte{}
		size, _ := strconv.Atoi(r.FormValue("size"))
		cs, _ := strconv.Atoi(r.FormValue("chunk_size"))
		total := (size + cs - 1) / cs
		d.meta[id] = [3]string{r.FormValue("path"), r.FormValue("name"), strconv.Itoa(total)}
		writeJSON(w, 200, map[string]any{"upload_id": id, "chunk_size": cs, "total_chunks": total})
	case "/api/upload/status":
		got, ok := d.chunks[q.Get("upload_id")]
		if !ok {
			fail(w, 404, "Upload session not found or expired")
			return
		}
		received := []int{}
		for i := range got {
			received = append(received, i)
		}
		sort.Ints(received)
		writeJSON(w, 200, map[string]any{"upload_id": q.Get("upload_id"), "received": received})
	case "/api/upload/fingerprint":
		f, ok := d.files[r.FormValue("path")]
		size, _ := strconv.Atoi(r.FormValue("size"))
		if !ok || f.isDir || len(f.data) != size {
			writeJSON(w, 200, map[string]any{"fingerprint": nil})
			return
		}
		writeJSON(w, 200, map[string]any{"fingerprint": fingerprintBytes(f.data)})
	case "/api/upload/chunk":
		index, _ := strconv.Atoi(q.Get("index"))
		d.chunkPuts++
		if d.failAlways[index] {
			fail(w, 502, "bad gateway")
			return
		}
		if d.failOnce[index] {
			delete(d.failOnce, index)
			fail(w, 502, "bad gateway")
			return
		}
		data, err := io.ReadAll(r.Body)
		if err != nil {
			fail(w, 400, "Incomplete chunk")
			return
		}
		if _, ok := d.chunks[q.Get("upload_id")]; !ok {
			fail(w, 404, "Upload session not found or expired")
			return
		}
		d.chunks[q.Get("upload_id")][index] = data
		writeJSON(w, 200, map[string]any{"ok": true})
	case "/api/upload/abort":
		d.aborts++
		delete(d.chunks, r.FormValue("upload_id"))
		writeJSON(w, 200, map[string]any{"ok": true})
	case "/api/mkdir":
		p := join(r.FormValue("path"), r.FormValue("name"))
		if _, exists := d.files[p]; exists {
			fail(w, 409, "Target exists")
			return
		}
		d.dir(p)
		writeJSON(w, 200, d.entry(p))
	case "/api/delete":
		if d.failDelete {
			fail(w, 403, "Permission denied")
			return
		}
		p := r.FormValue("path")
		delete(d.files, p)
		writeJSON(w, 200, map[string]any{"ok": true, "action": "recycled", "path": "#recycle/" + p})
	case "/api/move":
		src := r.FormValue("path")
		d.relocate(w, src, join(r.FormValue("dest"), path.Base(src)))
	case "/api/rename":
		src := r.FormValue("path")
		d.relocate(w, src, join(path.Dir("/" + src)[1:], r.FormValue("new_name")))
	case "/api/search":
		results := []map[string]any{}
		for p := range d.files {
			if p != "" && strings.Contains(p, q.Get("q")) {
				results = append(results, d.entry(p))
			}
		}
		writeJSON(w, 200, map[string]any{"query": q.Get("q"), "results": results})
	case "/api/cli/logout":
		d.loggedOut = true
		writeJSON(w, 200, map[string]any{"ok": true})
	default:
		fail(w, 404, "no route "+r.URL.Path)
	}
}

func (d *fakeDrive) completeUpload(w http.ResponseWriter, r *http.Request) {
	id := r.FormValue("upload_id")
	m := d.meta[id]
	total, _ := strconv.Atoi(m[2])
	if len(d.chunks[id]) != total {
		fail(w, 400, "Missing chunks")
		return
	}
	var data []byte
	for i := 0; i < total; i++ {
		data = append(data, d.chunks[id][i]...)
	}
	d.finishUpload(w, join(m[0], m[1]), data, r.FormValue("expect_mtime_ns"))
	delete(d.chunks, id)
}

// relocate moves an item and everything under it, refusing to overwrite.
func (d *fakeDrive) relocate(w http.ResponseWriter, src, dst string) {
	if _, ok := d.files[src]; !ok {
		fail(w, 404, "Not found")
		return
	}
	if _, exists := d.files[dst]; exists {
		fail(w, 409, "Target exists")
		return
	}
	moved := map[string]*fakeFile{}
	for p, f := range d.files {
		if p == src || strings.HasPrefix(p, src+"/") {
			moved[dst+strings.TrimPrefix(p, src)] = f
			delete(d.files, p)
		}
	}
	for p, f := range moved {
		d.files[p] = f
	}
	writeJSON(w, 200, map[string]any{"ok": true})
}

func (d *fakeDrive) finishUpload(w http.ResponseWriter, p string, data []byte, expect string) {
	if expect != "" {
		want, _ := strconv.ParseInt(expect, 10, 64)
		current := int64(-1)
		if f, ok := d.files[p]; ok {
			current = f.mtimeNS
		}
		if current != want {
			fail(w, 409, "File changed on the Drive since you opened it")
			return
		}
	}
	d.uploads++
	d.put(p, string(data))
	writeJSON(w, 200, d.entry(p))
}

// fingerprintBytes is the Drive's fingerprint (fsops.upload_fingerprint).
// requestedBytes is how many bytes a "bytes=a-b" or "bytes=a-" header asks for.
func requestedBytes(rng string, size int64) int64 {
	var from, to int64
	if n, _ := fmt.Sscanf(rng, "bytes=%d-%d", &from, &to); n == 2 {
		return min(to, size-1) - from + 1
	}
	return size - from
}

func fingerprintBytes(data []byte) string {
	var digests []byte
	for i := 0; i < len(data); i += 8 << 20 {
		sum := sha256.Sum256(data[i:min(i+8<<20, len(data))])
		digests = append(digests, sum[:]...)
	}
	total := sha256.Sum256(digests)
	return hex.EncodeToString(total[:])
}

// withLAN starts a second server for the same fake Drive that acts as its LAN
// address (requests there are marked as LAN). Close it to "leave the network".
func (d *fakeDrive) withLAN(t *testing.T) *httptest.Server {
	lan := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		r.Header.Set("X-Test-Lan", "1")
		d.serve(w, r)
	}))
	t.Cleanup(lan.Close)
	d.mu.Lock()
	d.lanURL = lan.URL
	d.mu.Unlock()
	return lan
}
