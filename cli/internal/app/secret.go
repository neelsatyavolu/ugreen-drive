package app

import (
	"fmt"
	"os"

	"golang.org/x/term"
)

// readSecret prompts on stderr and reads a line from the terminal without echo.
func readSecret(prompt string) (string, error) {
	fmt.Fprint(os.Stderr, prompt)
	raw, err := term.ReadPassword(int(os.Stdin.Fd()))
	fmt.Fprintln(os.Stderr)
	return string(raw), err
}
