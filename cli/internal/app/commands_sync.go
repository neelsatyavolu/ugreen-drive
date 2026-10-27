package app

import (
	"context"
	"errors"
	"flag"
	"fmt"
	"io"
	"net/http"
	"path"
	"sort"
	"strings"

	"github.com/neelsatyavolu/ugreen-drive/cli/internal/api"
	"github.com/neelsatyavolu/ugreen-drive/cli/internal/transfer"
)

// remoteIndex lists everything under root on the Drive, keyed by full path.
// A missing root is an empty index (sync will create it).
func remoteIndex(ctx context.Context, client *api.Client, root string) (map[string]api.Entry, error) {
	index := map[string]api.Entry{}
	var walkDir func(dir string) error
	walkDir = func(dir string) error {
		listing, err := client.List(ctx, dir)
		if err != nil {
			return err
		}
		for _, e := range listing.Items {
			index[e.Path] = e
			if e.IsDir && e.Name != "#recycle" {
				if err := walkDir(e.Path); err != nil {
					return err
				}
			}
		}
		return nil
	}
	err := walkDir(root)
	var apiErr *api.Error
	if errors.As(err, &apiErr) && apiErr.Status == http.StatusNotFound {
		return map[string]api.Entry{}, nil
	}
	return index, err
}

type syncPlan struct {
	Upload    []string `json:"upload"`
	Unchanged []string `json:"unchanged"`
	Conflicts []string `json:"conflict"`
	DriveOnly []string `json:"drive_only"`
	jobs      []uploadJob
	dirs      []string
}

func underAny(p string, dirs map[string]bool) bool {
	for d := range dirs {
		if p == d || strings.HasPrefix(p, d+"/") {
			return true
		}
	}
	return false
}

// planSync decides, per local file, whether the Drive needs it.
func planSync(ctx context.Context, client *api.Client, local transfer.Plan, root string, index map[string]api.Entry) (syncPlan, error) {
	plan := syncPlan{Upload: []string{}, Unchanged: []string{}, Conflicts: []string{}, DriveOnly: []string{}}
	blocked := map[string]bool{} // local folders that are files on the Drive
	for _, rel := range local.Dirs {
		full := path.Join(root, rel)
		if e, ok := index[full]; ok && !e.IsDir {
			blocked[full] = true
			plan.Conflicts = append(plan.Conflicts, full)
		} else if !ok {
			plan.dirs = append(plan.dirs, full)
		}
	}
	seen := map[string]bool{}
	for _, f := range local.Files {
		full := path.Join(root, f.Rel)
		f.Remote = full
		seen[full] = true
		if underAny(path.Dir(full), blocked) {
			plan.Conflicts = append(plan.Conflicts, full)
			continue
		}
		e, ok := index[full]
		switch {
		case !ok:
			must := api.MustNotExist
			plan.jobs = append(plan.jobs, uploadJob{file: f, expect: &must})
		case e.IsDir:
			plan.Conflicts = append(plan.Conflicts, full)
			continue
		case e.Size != f.Size:
			mtime := e.MtimeNS
			plan.jobs = append(plan.jobs, uploadJob{file: f, expect: &mtime})
		default:
			same, err := sameContent(ctx, client, f, full)
			if err != nil {
				return plan, err
			}
			if same {
				plan.Unchanged = append(plan.Unchanged, full)
				continue
			}
			mtime := e.MtimeNS
			plan.jobs = append(plan.jobs, uploadJob{file: f, expect: &mtime})
		}
		plan.Upload = append(plan.Upload, full)
	}
	for p, e := range index {
		if !e.IsDir && !seen[p] && !underAny(p, map[string]bool{path.Join(root, "#recycle"): true}) {
			plan.DriveOnly = append(plan.DriveOnly, p)
		}
	}
	for _, list := range [][]string{plan.Upload, plan.Unchanged, plan.Conflicts, plan.DriveOnly} {
		sort.Strings(list)
	}
	return plan, nil
}

func sameContent(ctx context.Context, client *api.Client, f transfer.File, remote string) (bool, error) {
	remoteFP, err := client.Fingerprint(ctx, remote, f.Size)
	if err != nil || remoteFP == "" {
		return false, err
	}
	localFP, err := transfer.Fingerprint(f.Local)
	if err != nil {
		return false, err
	}
	return localFP == remoteFP, nil
}

func cmdSync(ctx context.Context, r *runner, args []string) error {
	fs := flag.NewFlagSet("sync", flag.ContinueOnError)
	dryRun := fs.Bool("dry-run", false, "show what would be uploaded")
	rest, err := parseFlags(fs, args)
	if err != nil {
		return err
	}
	if len(rest) != 2 {
		return usagef("usage: ugd sync LOCAL_DIR REMOTE_DIR [--dry-run]")
	}
	local, err := transfer.Walk(rest[0])
	if err != nil {
		return usageError{err.Error()}
	}
	root := api.CleanPath(rest[1])
	client, err := r.signedIn()
	if err != nil {
		return err
	}
	index, err := remoteIndex(ctx, client, root)
	if err != nil {
		return err
	}
	plan, err := planSync(ctx, client, local, root, index)
	if err != nil {
		return err
	}

	if *dryRun {
		if err := r.emit(plan, func(w io.Writer) {
			for _, p := range plan.Upload {
				fmt.Fprintf(w, "upload     %s\n", p)
			}
			for _, p := range plan.Conflicts {
				fmt.Fprintf(w, "conflict   %s\n", p)
			}
			fmt.Fprintf(w, "%d to upload, %d unchanged, %d conflict(s), %d only on the Drive (never deleted)\n",
				len(plan.Upload), len(plan.Unchanged), len(plan.Conflicts), len(plan.DriveOnly))
		}); err != nil {
			return err
		}
		if len(plan.Conflicts) > 0 {
			return exitError{ExitConflict, fmt.Sprintf("%d conflict(s)", len(plan.Conflicts))}
		}
		return nil
	}

	known := map[string]bool{"": true}
	for p, e := range index {
		if e.IsDir {
			known[p] = true
		}
	}
	if err := mkdirAll(ctx, client, append([]string{root}, plan.dirs...), known); err != nil {
		return err
	}
	result := r.runUploads(ctx, client, plan.jobs)
	result.Conflicts = append(result.Conflicts, plan.Conflicts...)
	result.Unchanged = plan.Unchanged
	result.DriveOnly = plan.DriveOnly
	result.Skipped = append(local.Skipped, local.Symlinks...)
	return r.finishBatch(result, "the Drive copy changed; re-run sync to compare again")
}
