package api

import (
	"context"
	"net/url"
	"strconv"
)

// PersonalStatus is an encrypted personal folder's state (UGOS encryption,
// unlocked for 24 hours at a time).
type PersonalStatus struct {
	Encrypted bool     `json:"encrypted"`
	Locked    bool     `json:"locked"`
	ExpiresAt *float64 `json:"expires_at"` // Unix seconds when it relocks
}

// PersonalAuth is the result of signing in to the NAS as a personal-folder
// owner. NeedOTP means: send the authenticator code with Pending.
type PersonalAuth struct {
	OK      bool   `json:"ok"`
	NeedOTP bool   `json:"need_otp"`
	Pending string `json:"pending,omitempty"`
}

// UnlockPersonal unlocks the owner's encrypted personal folder with its
// encryption password, or the contents of its key file (keyFile). A 428
// error means UGOS first needs AuthPersonal.
func (c *Client) UnlockPersonal(ctx context.Context, owner, key string, keyFile bool) (PersonalStatus, error) {
	var status PersonalStatus
	err := c.postForm(ctx, "/api/personal/unlock", url.Values{
		"owner": {owner}, "key": {key}, "key_file": {strconv.FormatBool(keyFile)},
	}, &status)
	return status, err
}

// AuthPersonal signs in to the NAS as the folder owner so UGOS allows
// unlocking and relocking. Send the password first; if NeedOTP, call again
// with the authenticator code and the returned Pending.
func (c *Client) AuthPersonal(ctx context.Context, owner, password, code, pending string) (PersonalAuth, error) {
	var auth PersonalAuth
	err := c.postForm(ctx, "/api/personal/auth", url.Values{
		"owner": {owner}, "password": {password}, "code": {code}, "pending": {pending},
	}, &auth)
	return auth, err
}
