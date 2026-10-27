package app

import (
	"bufio"
	"context"
	"errors"
	"flag"
	"fmt"
	"io"
	"net/http"
	"strings"
	"text/tabwriter"
	"time"

	"github.com/neelsatyavolu/ugreen-drive/cli/internal/api"
)

func formatSize(n int64) string {
	units := []string{"B", "KB", "MB", "GB", "TB"}
	size, unit := float64(n), 0
	for size >= 1024 && unit < len(units)-1 {
		size /= 1024
		unit++
	}
	if unit == 0 {
		return fmt.Sprintf("%d B", n)
	}
	return fmt.Sprintf("%.1f %s", size, units[unit])
}

func formatTime(iso string) string {
	t, err := time.Parse(time.RFC3339Nano, iso)
	if err != nil {
		return iso
	}
	return t.Local().Format("2006-01-02 15:04")
}

func printEntries(w io.Writer, items []api.Entry) {
	tw := tabwriter.NewWriter(w, 0, 0, 2, ' ', 0)
	for _, item := range items {
		size, name := formatSize(item.Size), item.Name
		if item.IsDir {
			size, name = "-", name+"/"
		}
		fmt.Fprintf(tw, "%s\t%s\t%s\n", size, formatTime(item.Mtime), name)
	}
	tw.Flush()
}

func onePath(args []string, usage string) (string, error) {
	switch len(args) {
	case 0:
		return "", nil
	case 1:
		return args[0], nil
	}
	return "", usagef("usage: ugd %s", usage)
}

func cmdLs(ctx context.Context, r *runner, args []string) error {
	dir, err := onePath(args, "ls [PATH]")
	if err != nil {
		return err
	}
	client, err := r.signedIn()
	if err != nil {
		return err
	}
	listing, err := client.List(ctx, dir)
	if err != nil {
		return err
	}
	return r.emit(listing, func(w io.Writer) { printEntries(w, listing.Items) })
}

type treeNode struct {
	api.Entry
	Children []treeNode `json:"children,omitempty"`
}

func walk(ctx context.Context, client *api.Client, dir string, depth int) ([]treeNode, error) {
	listing, err := client.List(ctx, dir)
	if err != nil {
		return nil, err
	}
	nodes := make([]treeNode, 0, len(listing.Items))
	for _, item := range listing.Items {
		node := treeNode{Entry: item}
		if item.IsDir && depth > 1 && item.Name != "#recycle" {
			if node.Children, err = walk(ctx, client, item.Path, depth-1); err != nil {
				return nil, err
			}
		}
		nodes = append(nodes, node)
	}
	return nodes, nil
}

func printTree(w io.Writer, nodes []treeNode, indent string) {
	for _, node := range nodes {
		name := node.Name
		if node.IsDir {
			name += "/"
		}
		fmt.Fprintln(w, indent+name)
		printTree(w, node.Children, indent+"  ")
	}
}

func cmdTree(ctx context.Context, r *runner, args []string) error {
	fs := flag.NewFlagSet("tree", flag.ContinueOnError)
	depth := fs.Int("depth", 3, "levels to descend")
	rest, err := parseFlags(fs, args)
	if err != nil {
		return err
	}
	dir, err := onePath(rest, "tree [PATH] [--depth N]")
	if err != nil {
		return err
	}
	if *depth < 1 || *depth > 20 {
		return usagef("--depth must be between 1 and 20")
	}
	client, err := r.signedIn()
	if err != nil {
		return err
	}
	nodes, err := walk(ctx, client, dir, *depth)
	if err != nil {
		return err
	}
	return r.emit(map[string]any{"path": api.CleanPath(dir), "items": nodes}, func(w io.Writer) {
		printTree(w, nodes, "")
	})
}

func cmdSearch(ctx context.Context, r *runner, args []string) error {
	fs := flag.NewFlagSet("search", flag.ContinueOnError)
	under := fs.String("path", "", "folder to search in")
	limit := fs.Int("limit", 50, "maximum results")
	rest, err := parseFlags(fs, args)
	if err != nil {
		return err
	}
	if len(rest) == 0 {
		return usagef("usage: ugd search QUERY [--path P] [--limit N]")
	}
	client, err := r.signedIn()
	if err != nil {
		return err
	}
	result, err := client.Search(ctx, strings.Join(rest, " "), *under, *limit)
	if err != nil {
		return err
	}
	return r.emit(result, func(w io.Writer) {
		for _, item := range result.Results {
			suffix := ""
			if item.IsDir {
				suffix = "/"
			}
			fmt.Fprintln(w, item.Path+suffix)
		}
		if result.HasMore {
			fmt.Fprintln(w, "(more results; narrow the query or raise --limit)")
		}
	})
}

func cmdMkdir(ctx context.Context, r *runner, args []string) error {
	fs := flag.NewFlagSet("mkdir", flag.ContinueOnError)
	parents := fs.Bool("p", false, "create parents; ok if it exists")
	rest, err := parseFlags(fs, args)
	if err != nil {
		return err
	}
	if len(rest) != 1 || api.CleanPath(rest[0]) == "" {
		return usagef("usage: ugd mkdir PATH [-p]")
	}
	client, err := r.signedIn()
	if err != nil {
		return err
	}
	target := api.CleanPath(rest[0])
	parts := []string{target}
	if *parents {
		parts = parts[:0]
		segments := strings.Split(target, "/")
		for i := range segments {
			parts = append(parts, strings.Join(segments[:i+1], "/"))
		}
	}
	for _, p := range parts {
		dir, name := api.SplitPath(p)
		_, err := client.Mkdir(ctx, dir, name)
		var apiErr *api.Error
		if *parents && errors.As(err, &apiErr) && apiErr.Status == http.StatusConflict {
			entry, found, statErr := client.Stat(ctx, p)
			if statErr == nil && found && entry.IsDir {
				continue
			}
		}
		if err != nil {
			return err
		}
	}
	return r.emit(map[string]string{"path": target}, func(w io.Writer) {
		fmt.Fprintf(w, "Created %s/\n", target)
	})
}

func cmdMv(ctx context.Context, r *runner, args []string) error {
	if len(args) < 2 {
		return usagef("usage: ugd mv SRC... DEST_FOLDER")
	}
	client, err := r.signedIn()
	if err != nil {
		return err
	}
	dest := args[len(args)-1]
	moved := []string{}
	for _, src := range args[:len(args)-1] {
		if err := client.Move(ctx, src, dest); err != nil {
			return fmt.Errorf("move %s: %w", api.CleanPath(src), err)
		}
		moved = append(moved, api.CleanPath(src))
	}
	return r.emit(map[string]any{"moved": moved, "dest": api.CleanPath(dest)}, func(w io.Writer) {
		for _, src := range moved {
			fmt.Fprintf(w, "Moved %s → %s/\n", src, api.CleanPath(dest))
		}
	})
}

func cmdRename(ctx context.Context, r *runner, args []string) error {
	if len(args) != 2 || strings.Contains(args[1], "/") {
		return usagef("usage: ugd rename PATH NEW_NAME (NEW_NAME is a name, not a path)")
	}
	client, err := r.signedIn()
	if err != nil {
		return err
	}
	if err := client.Rename(ctx, args[0], args[1]); err != nil {
		return err
	}
	return r.emit(map[string]string{"path": api.CleanPath(args[0]), "new_name": args[1]}, func(w io.Writer) {
		fmt.Fprintf(w, "Renamed %s → %s\n", api.CleanPath(args[0]), args[1])
	})
}

func cmdRm(ctx context.Context, r *runner, args []string) error {
	fs := flag.NewFlagSet("rm", flag.ContinueOnError)
	yes := fs.Bool("y", false, "don't ask for confirmation")
	rest, err := parseFlags(fs, args)
	if err != nil {
		return err
	}
	if len(rest) == 0 {
		return usagef("usage: ugd rm PATH... [-y]")
	}
	for _, p := range rest {
		if api.CleanPath(p) == "" {
			return usagef("refusing to remove the share root")
		}
	}
	client, err := r.signedIn()
	if err != nil {
		return err
	}
	if r.env.StdinIsTTY && !*yes && !r.g.json {
		fmt.Fprintf(r.env.Stderr, "Remove %d item(s) (to the Recycle bin when the share has one)? [y/N] ", len(rest))
		answer, _ := bufio.NewReader(r.env.Stdin).ReadString('\n')
		if a := strings.ToLower(strings.TrimSpace(answer)); a != "y" && a != "yes" {
			return exitError{ExitError, "cancelled"}
		}
	}
	results := []api.DeleteResult{}
	for _, p := range rest {
		result, err := client.Delete(ctx, p)
		if err != nil {
			return fmt.Errorf("remove %s: %w", api.CleanPath(p), err)
		}
		results = append(results, result)
	}
	return r.emit(map[string]any{"removed": results}, func(w io.Writer) {
		for i, result := range results {
			if result.Action == "recycled" {
				fmt.Fprintf(w, "Moved %s to the Recycle bin\n", api.CleanPath(rest[i]))
			} else {
				fmt.Fprintf(w, "Deleted %s permanently\n", api.CleanPath(rest[i]))
			}
		}
	})
}
