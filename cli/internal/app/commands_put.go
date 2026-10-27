package app

import (
	"context"
	"errors"
	"flag"
	"fmt"
	"io"
	"net/http"
	"os"
	"path"
	"path/filepath"
	"strings"
	"sync"
	"time"

	"github.com/neelsatyavolu/ugreen-drive/cli/internal/api"
	"github.com/neelsatyavolu/ugreen-drive/cli/internal/transfer"
)

const parallelFiles = 3

func cmdPut(ctx context.Context, r *runner, args []string) error {
	fs := flag.NewFlagSet("put", flag.ContinueOnError)
	recursive := fs.Bool("r", false, "upload folders recursively")
	force := fs.Bool("force", false, "overwrite existing files")
	expect := fs.Int64("expect-mtime-ns", 0, "only overwrite if the Drive copy still has this mtime_ns")
	rest, err := parseFlags(fs, args)
	if err != nil {
		return err
	}
	if len(rest) < 2 {
		return usagef("usage: ugd put [-r] [--force] SRC... DEST")
	}
	if *force && *expect != 0 {
		return usagef("use either --force or --expect-mtime-ns, not both")
	}
	sources, dest := rest[:len(rest)-1], rest[len(rest)-1]
	if len(sources) == 1 && !*recursive {
		return putOne(ctx, r, sources[0], dest, *force, *expect)
	}
	for _, src := range sources {
		if src == "-" {
			return usagef("stdin (-) can only be uploaded on its own")
		}
	}
	if *expect != 0 {
		return usagef("--expect-mtime-ns works with a single file only")
	}
	plan, err := transfer.Collect(sources, api.CleanPath(dest), *recursive)
	if err != nil {
		return usageError{err.Error()}
	}
	client, err := r.signedIn()
	if err != nil {
		return err
	}
	if err := mkdirAll(ctx, client, plan.Dirs, nil); err != nil {
		return err
	}
	existing := map[string]bool{}
	if !*force {
		// Skip files already on the Drive before sending a single byte; the
		// server's create-only check at the end still catches races.
		if existing, err = existingFiles(ctx, client, plan.Files); err != nil {
			return err
		}
	}
	jobs := make([]uploadJob, 0, len(plan.Files))
	var skippedExisting []string
	for _, f := range plan.Files {
		if existing[f.Remote] {
			skippedExisting = append(skippedExisting, f.Remote)
			continue
		}
		jobs = append(jobs, uploadJob{file: f, expect: createOnly(*force)})
	}
	result := r.runUploads(ctx, client, jobs)
	result.Exists = append(skippedExisting, result.Exists...)
	result.Skipped = append(plan.Skipped, plan.Symlinks...)
	return r.finishBatch(result, "use --force to overwrite")
}

func createOnly(force bool) *int64 {
	if force {
		return nil
	}
	v := api.MustNotExist
	return &v
}

// putOne keeps put's single-file behavior: DEST may be a folder or a file path.
func putOne(ctx context.Context, r *runner, source, dest string, force bool, expect int64) error {
	client, err := r.signedIn()
	if err != nil {
		return err
	}
	var file *os.File
	if source == "-" {
		file, err = stageStdin(r.env.Stdin)
	} else {
		file, err = os.Open(source)
	}
	if err != nil {
		return err
	}
	defer file.Close()
	info, err := file.Stat()
	if err != nil {
		return err
	}
	if info.IsDir() {
		return usagef("%s is a folder; use -r to upload folders", source)
	}

	dir, name := api.SplitPath(dest)
	if strings.HasSuffix(dest, "/") || api.CleanPath(dest) == "" {
		dir, name = api.CleanPath(dest), filepath.Base(source)
	} else if entry, found, err := client.Stat(ctx, dest); err != nil {
		return err
	} else if found && entry.IsDir {
		dir, name = entry.Path, filepath.Base(source)
	} else if found && !force && expect == 0 {
		// Fail before sending any bytes; the server re-checks atomically anyway.
		return &api.Error{Status: 409, Detail: api.CleanPath(dest) + " already exists (use --force to overwrite)"}
	}
	if source == "-" && name == "-" {
		return usagef("give a file name when uploading from stdin")
	}

	opts := api.UploadOptions{ExpectMtimeNS: createOnly(force)}
	if expect != 0 {
		opts.ExpectMtimeNS = &expect
	}
	remote := path.Join(dir, name)
	prog := r.newProgress(1, info.Size())
	prog.fileStarted(name)
	opts.Progress = prog.add
	opts.OnResume = func(done, total int64) {
		prog.note("Resuming %s (%d%% already on the Drive)", name, done*100/max(total, 1))
	}
	if source != "-" {
		abs, _ := filepath.Abs(source)
		opts.Resume = r.resumeFor(client, remote, abs, info)
	}
	entry, err := client.UploadFile(ctx, dir, name, file, opts)
	prog.finish()
	var apiErr *api.Error
	if errors.As(err, &apiErr) && apiErr.Status == http.StatusConflict && !force && expect == 0 {
		return &api.Error{Status: http.StatusConflict, Detail: apiErr.Detail + " (use --force to overwrite)"}
	}
	if err != nil {
		return err
	}
	return r.emit(entry, func(w io.Writer) {
		fmt.Fprintf(w, "Uploaded %s (%s)\n", entry.Path, formatSize(entry.Size))
	})
}

// existingFiles lists each destination folder once and reports which planned
// files already exist there.
func existingFiles(ctx context.Context, client *api.Client, files []transfer.File) (map[string]bool, error) {
	listed := map[string]bool{}
	existing := map[string]bool{}
	for _, f := range files {
		dir, _ := api.SplitPath(f.Remote)
		if listed[dir] {
			continue
		}
		listed[dir] = true
		listing, err := client.List(ctx, dir)
		var apiErr *api.Error
		if errors.As(err, &apiErr) && apiErr.Status == http.StatusNotFound {
			continue
		}
		if err != nil {
			return nil, err
		}
		for _, item := range listing.Items {
			existing[item.Path] = true
		}
	}
	return existing, nil
}

// mkdirAll creates each Drive folder (and its parents); known caches folders
// that already exist. A path that exists as a file is an error.
func mkdirAll(ctx context.Context, client *api.Client, dirs []string, known map[string]bool) error {
	if known == nil {
		known = map[string]bool{"": true}
	}
	for _, target := range dirs {
		segments := strings.Split(api.CleanPath(target), "/")
		for i := range segments {
			p := strings.Join(segments[:i+1], "/")
			if p == "" || known[p] {
				continue
			}
			parent, name := api.SplitPath(p)
			_, err := client.Mkdir(ctx, parent, name)
			var apiErr *api.Error
			if errors.As(err, &apiErr) && apiErr.Status == http.StatusConflict {
				entry, found, statErr := client.Stat(ctx, p)
				if statErr != nil {
					return statErr
				}
				if !found || !entry.IsDir {
					return &api.Error{Status: http.StatusConflict, Detail: p + " exists on the Drive and is not a folder"}
				}
				err = nil
			}
			if err != nil {
				return fmt.Errorf("create folder %s: %w", p, err)
			}
			known[p] = true
		}
	}
	return nil
}

// resumeFile adapts transfer.Store to one file's api.ResumeStore.
type resumeFile struct {
	store transfer.Store
	key   string
}

func (r resumeFile) Load() (api.ChunkSession, bool) {
	st, ok := r.store.Load(r.key)
	return api.ChunkSession{UploadID: st.UploadID, ChunkSize: st.ChunkSize, TotalChunks: st.TotalChunks}, ok
}

func (r resumeFile) Save(s api.ChunkSession) error {
	return r.store.Save(r.key, transfer.State{UploadID: s.UploadID, ChunkSize: s.ChunkSize, TotalChunks: s.TotalChunks})
}

func (r resumeFile) Clear() { r.store.Delete(r.key) }

func (r *runner) resumeStore() transfer.Store {
	return transfer.Store{Dir: filepath.Join(r.env.ConfigDir, "uploads")}
}

func (r *runner) resumeFor(client *api.Client, remote, local string, info os.FileInfo) api.ResumeStore {
	key := transfer.ResumeKey(client.Base.String(), client.Share, remote, local, info.Size(), info.ModTime().UnixNano())
	return resumeFile{store: r.resumeStore(), key: key}
}

type uploadJob struct {
	file   transfer.File
	expect *int64 // nil = overwrite; api.MustNotExist = create only; else Drive mtime_ns
}

type failedUpload struct {
	Path  string `json:"path"`
	Error string `json:"error"`
}

type batchResult struct {
	Uploaded  []string       `json:"uploaded"`
	Exists    []string       `json:"exists,omitempty"`
	Conflicts []string       `json:"conflict,omitempty"`
	Failed    []failedUpload `json:"failed,omitempty"`
	Skipped   []string       `json:"skipped,omitempty"`
	Unchanged []string       `json:"unchanged,omitempty"`
	DriveOnly []string       `json:"drive_only,omitempty"`
	Bytes     int64          `json:"bytes"`
	authErr   error
	elapsed   time.Duration
}

// runUploads sends jobs, parallelFiles at a time, and classifies each outcome.
func (r *runner) runUploads(ctx context.Context, client *api.Client, jobs []uploadJob) batchResult {
	result := batchResult{Uploaded: []string{}}
	var total int64
	for _, j := range jobs {
		total += j.file.Size
	}
	r.resumeStore().Prune(24 * time.Hour)
	prog := r.newProgress(len(jobs), total)
	started := time.Now()
	var mu sync.Mutex
	sem := make(chan struct{}, parallelFiles)
	var wg sync.WaitGroup
	for _, job := range jobs {
		if ctx.Err() != nil {
			break
		}
		wg.Add(1)
		sem <- struct{}{}
		go func(job uploadJob) {
			defer func() { <-sem; wg.Done() }()
			err := r.uploadJobFile(ctx, client, job, prog)
			prog.fileDone()
			mu.Lock()
			defer mu.Unlock()
			result.classify(job, err)
		}(job)
	}
	wg.Wait()
	prog.finish()
	result.elapsed = time.Since(started)
	return result
}

func (r *runner) uploadJobFile(ctx context.Context, client *api.Client, job uploadJob, prog *progress) error {
	f, err := os.Open(job.file.Local)
	if err != nil {
		return err
	}
	defer f.Close()
	info, err := f.Stat()
	if err != nil {
		return err
	}
	dir, name := api.SplitPath(job.file.Remote)
	prog.fileStarted(job.file.Remote)
	_, err = client.UploadFile(ctx, dir, name, f, api.UploadOptions{
		ExpectMtimeNS: job.expect,
		Resume:        r.resumeFor(client, job.file.Remote, job.file.Local, info),
		Progress:      prog.add,
		OnResume: func(done, total int64) {
			prog.note("Resuming %s (%d%% already on the Drive)", job.file.Remote, done*100/max(total, 1))
		},
	})
	return err
}

func (b *batchResult) classify(job uploadJob, err error) {
	remote := job.file.Remote
	var apiErr *api.Error
	switch {
	case err == nil:
		b.Uploaded = append(b.Uploaded, remote)
		b.Bytes += job.file.Size
	case errors.As(err, &apiErr) && apiErr.Status == http.StatusConflict:
		if job.expect != nil && *job.expect == api.MustNotExist {
			b.Exists = append(b.Exists, remote)
		} else {
			b.Conflicts = append(b.Conflicts, remote)
		}
	case errors.As(err, &apiErr) && apiErr.Status == http.StatusUnauthorized:
		b.authErr = err
		b.Failed = append(b.Failed, failedUpload{remote, err.Error()})
	default:
		b.Failed = append(b.Failed, failedUpload{remote, err.Error()})
	}
}

// finishBatch prints the summary and turns the outcome into an exit code.
func (r *runner) finishBatch(b batchResult, existsHint string) error {
	if err := r.emit(b, func(w io.Writer) {
		fmt.Fprintf(w, "Uploaded %d file(s) (%s) in %s\n", len(b.Uploaded), formatSize(b.Bytes), b.elapsed.Round(time.Second))
		if len(b.Unchanged) > 0 {
			fmt.Fprintf(w, "Unchanged: %d file(s)\n", len(b.Unchanged))
		}
		if len(b.DriveOnly) > 0 {
			fmt.Fprintf(w, "Only on the Drive (left alone): %d file(s)\n", len(b.DriveOnly))
		}
		for _, p := range b.Exists {
			fmt.Fprintf(w, "Exists, skipped: %s\n", p)
		}
		for _, p := range b.Conflicts {
			fmt.Fprintf(w, "Conflict, skipped: %s\n", p)
		}
		for _, f := range b.Failed {
			fmt.Fprintf(w, "Failed: %s: %s\n", f.Path, f.Error)
		}
		if len(b.Skipped) > 0 {
			fmt.Fprintf(w, "Not uploaded (system files or symlinks): %d\n", len(b.Skipped))
		}
	}); err != nil {
		return err
	}
	switch {
	case b.authErr != nil:
		return b.authErr
	case len(b.Failed) > 0:
		return exitError{ExitError, fmt.Sprintf("%d upload(s) failed; re-run the same command to resume", len(b.Failed))}
	case len(b.Exists) > 0:
		return exitError{ExitConflict, fmt.Sprintf("%d file(s) already exist on the Drive (%s)", len(b.Exists), existsHint)}
	case len(b.Conflicts) > 0:
		return exitError{ExitConflict, fmt.Sprintf("%d conflict(s): the Drive has a different item there", len(b.Conflicts))}
	}
	return nil
}
