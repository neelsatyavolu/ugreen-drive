// Package transfer holds the local side of uploads: which files to send and
// where they go, content fingerprints, and resumable-upload state.
package transfer

import (
	"fmt"
	"io/fs"
	"os"
	"path"
	"path/filepath"
	"strings"
)

// Skip reports whether a local file or folder name should never be uploaded.
// It mirrors the Drive's hidden-junk filter (fsops._is_hidden_entry) plus
// macOS metadata the Drive would otherwise show.
func Skip(name string) bool {
	switch name {
	case ".DS_Store", "Thumbs.db", "desktop.ini", ".Spotlight-V100", ".Trashes", ".fseventsd", ".TemporaryItems":
		return true
	}
	if strings.HasPrefix(name, "._") || strings.HasPrefix(name, ".ifd-") {
		return true
	}
	lower := strings.ToLower(name)
	for _, suffix := range []string{".ug-tmp", ".ugtmp", ".tmp", ".partial", ".crdownload"} {
		if strings.HasSuffix(lower, suffix) {
			return true
		}
	}
	return false
}

// File is one local file and the Drive path it uploads to.
type File struct {
	Local   string // absolute local path
	Rel     string // path relative to the source root ("a.mov", "Day 2/b.mov")
	Remote  string // full Drive path, e.g. "Documents/Ep1/Footage/a.mov"
	Size    int64
	ModTime int64 // UnixNano, part of the resume key
}

// Plan is everything an upload will create on the Drive.
type Plan struct {
	Files    []File
	Dirs     []string // Drive folders to ensure, parents before children
	Skipped  []string // junk files left out
	Symlinks []string // symlinks left out
}

func joinRemote(parts ...string) string {
	kept := parts[:0:0]
	for _, p := range parts {
		if p = strings.Trim(p, "/"); p != "" {
			kept = append(kept, p)
		}
	}
	return path.Join(kept...)
}

// Collect expands put's sources into files under the Drive folder dest.
// Folders need recursive; each lands in dest/<folder name>/ like `cp -r`.
func Collect(sources []string, dest string, recursive bool) (Plan, error) {
	plan := Plan{Dirs: []string{joinRemote(dest)}}
	for _, src := range sources {
		abs, err := filepath.Abs(src)
		if err != nil {
			return Plan{}, err
		}
		info, err := os.Lstat(abs)
		if err != nil {
			return Plan{}, err
		}
		switch {
		case info.Mode()&os.ModeSymlink != 0:
			plan.Symlinks = append(plan.Symlinks, src)
		case info.IsDir():
			if !recursive {
				return Plan{}, fmt.Errorf("%s is a folder; use -r to upload folders", src)
			}
			if err := walkInto(&plan, abs, joinRemote(dest, filepath.Base(abs))); err != nil {
				return Plan{}, err
			}
		case info.Mode().IsRegular():
			plan.Files = append(plan.Files, File{
				Local: abs, Rel: filepath.Base(abs), Remote: joinRemote(dest, filepath.Base(abs)),
				Size: info.Size(), ModTime: info.ModTime().UnixNano(),
			})
		default:
			return Plan{}, fmt.Errorf("%s is not a regular file", src)
		}
	}
	return plan, nil
}

// Walk lists a local folder for sync: files relative to root, plus subfolders.
func Walk(root string) (Plan, error) {
	abs, err := filepath.Abs(root)
	if err != nil {
		return Plan{}, err
	}
	info, err := os.Stat(abs)
	if err != nil {
		return Plan{}, err
	}
	if !info.IsDir() {
		return Plan{}, fmt.Errorf("%s is not a folder", root)
	}
	plan := Plan{}
	return plan, walkInto(&plan, abs, "")
}

// walkInto adds everything under localRoot, mapping it below remoteRoot.
func walkInto(plan *Plan, localRoot, remoteRoot string) error {
	if remoteRoot != "" {
		plan.Dirs = append(plan.Dirs, remoteRoot)
	}
	return filepath.WalkDir(localRoot, func(p string, d fs.DirEntry, err error) error {
		if err != nil {
			return err
		}
		if p == localRoot {
			return nil
		}
		rel, err := filepath.Rel(localRoot, p)
		if err != nil {
			return err
		}
		rel = filepath.ToSlash(rel)
		if Skip(d.Name()) {
			plan.Skipped = append(plan.Skipped, rel)
			if d.IsDir() {
				return filepath.SkipDir
			}
			return nil
		}
		if d.Type()&fs.ModeSymlink != 0 {
			plan.Symlinks = append(plan.Symlinks, rel)
			return nil
		}
		if d.IsDir() {
			plan.Dirs = append(plan.Dirs, joinRemote(remoteRoot, rel))
			return nil
		}
		if !d.Type().IsRegular() {
			return nil
		}
		info, err := d.Info()
		if err != nil {
			return err
		}
		plan.Files = append(plan.Files, File{
			Local: p, Rel: rel, Remote: joinRemote(remoteRoot, rel),
			Size: info.Size(), ModTime: info.ModTime().UnixNano(),
		})
		return nil
	})
}
