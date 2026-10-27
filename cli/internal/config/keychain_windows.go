//go:build windows

package config

import (
	"errors"
	"fmt"
	"unsafe"

	"golang.org/x/sys/windows"
)

// Keychain stores tokens in Windows Credential Manager (the user's generic
// credentials, like `cmdkey`), one per Drive host: "ugreen-drive:<host>".
type Keychain struct{}

var (
	advapi32       = windows.NewLazySystemDLL("advapi32.dll")
	procCredReadW  = advapi32.NewProc("CredReadW")
	procCredWriteW = advapi32.NewProc("CredWriteW")
	procCredDelete = advapi32.NewProc("CredDeleteW")
	procCredFree   = advapi32.NewProc("CredFree")
)

const (
	credTypeGeneric      = 1
	credPersistLocalMach = 2 // survives logoff; per user, not shared with others
)

// credential mirrors the Win32 CREDENTIALW struct.
type credential struct {
	Flags              uint32
	Type               uint32
	TargetName         *uint16
	Comment            *uint16
	LastWritten        windows.Filetime
	CredentialBlobSize uint32
	CredentialBlob     *byte
	Persist            uint32
	AttributeCount     uint32
	Attributes         uintptr
	TargetAlias        *uint16
	UserName           *uint16
}

func credTarget(host string) (*uint16, error) {
	if !safeHost.MatchString(host) {
		return nil, fmt.Errorf("invalid server host %q", host)
	}
	return windows.UTF16PtrFromString(keychainService + ":" + host)
}

func (Keychain) Get(host string) (string, error) {
	target, err := credTarget(host)
	if err != nil {
		return "", err
	}
	var cred *credential
	r, _, callErr := procCredReadW.Call(uintptr(unsafe.Pointer(target)), credTypeGeneric, 0, uintptr(unsafe.Pointer(&cred)))
	if r == 0 {
		if errors.Is(callErr, windows.ERROR_NOT_FOUND) {
			return "", ErrNoToken
		}
		return "", fmt.Errorf("read Credential Manager: %w", callErr)
	}
	defer procCredFree.Call(uintptr(unsafe.Pointer(cred)))
	blob := unsafe.Slice(cred.CredentialBlob, cred.CredentialBlobSize)
	return string(blob), nil
}

func (Keychain) Set(host, token string) error {
	if !safeToken.MatchString(token) {
		return errors.New("refusing to store malformed server host or token")
	}
	target, err := credTarget(host)
	if err != nil {
		return err
	}
	user, _ := windows.UTF16PtrFromString("ugreen-drive")
	comment, _ := windows.UTF16PtrFromString("Ugreen Drive CLI")
	blob := []byte(token)
	cred := credential{
		Type:               credTypeGeneric,
		TargetName:         target,
		Comment:            comment,
		CredentialBlobSize: uint32(len(blob)),
		CredentialBlob:     &blob[0],
		Persist:            credPersistLocalMach,
		UserName:           user,
	}
	if r, _, callErr := procCredWriteW.Call(uintptr(unsafe.Pointer(&cred)), 0); r == 0 {
		return fmt.Errorf("save to Credential Manager: %w", callErr)
	}
	return nil
}

func (Keychain) Delete(host string) error {
	target, err := credTarget(host)
	if err != nil {
		return err
	}
	r, _, callErr := procCredDelete.Call(uintptr(unsafe.Pointer(target)), credTypeGeneric, 0)
	if r == 0 && !errors.Is(callErr, windows.ERROR_NOT_FOUND) {
		return fmt.Errorf("remove from Credential Manager: %w", callErr)
	}
	return nil
}
