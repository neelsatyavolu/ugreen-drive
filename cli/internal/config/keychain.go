package config

import (
	"errors"
	"regexp"
)

// ErrNoToken means this server has no saved sign-in.
var ErrNoToken = errors.New("not signed in")

// TokenStore keeps one CLI token per Drive server host.
type TokenStore interface {
	Get(host string) (string, error)
	Set(host, token string) error
	Delete(host string) error
}

const keychainService = "ugreen-drive"

var (
	safeHost  = regexp.MustCompile(`^[A-Za-z0-9.:-]+$`)
	safeToken = regexp.MustCompile(`^ugd_[A-Za-z0-9_-]+$`)
)

// MemoryStore is an in-process TokenStore for tests.
type MemoryStore map[string]string

func (m MemoryStore) Get(host string) (string, error) {
	if token, ok := m[host]; ok {
		return token, nil
	}
	return "", ErrNoToken
}

func (m MemoryStore) Set(host, token string) error { m[host] = token; return nil }

func (m MemoryStore) Delete(host string) error { delete(m, host); return nil }
