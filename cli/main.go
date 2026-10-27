// Command ugd is the Ugreen Drive command-line client.
package main

import (
	"context"
	"fmt"
	"os"
	"os/signal"

	"github.com/neelsatyavolu/ugreen-drive/cli/internal/app"
)

func main() {
	env, err := app.DefaultEnv()
	if err != nil {
		fmt.Fprintf(os.Stderr, "ugd: %v\n", err)
		os.Exit(app.ExitError)
	}
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt)
	code := app.Run(ctx, os.Args[1:], env)
	stop()
	os.Exit(code)
}
