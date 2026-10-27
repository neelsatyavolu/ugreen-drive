# Finder and Windows network drive (SMB)

The web drive is for browsers. To mount the NAS as a disk in Finder or File Explorer, use the NAS's own SMB server with your NAS username and password. The drive's sidebar has a **Connect in Finder** button that shows the address.

The web address (for example `https://drive.example.com`) is not an SMB server. Don't type it into Connect to Server.

## Mac

1. Finder → **Go → Connect to Server…** (`⌘K`).
2. Enter `smb://192.168.1.50`, using your NAS's LAN IP. Leave the folder name off so Finder lists every shared folder you can open.
3. Sign in with your NAS username and password.

## Windows

1. File Explorer → **This PC → Map network drive**.
2. Folder: `\\192.168.1.50\SharedFolderName`.
3. Sign in with your NAS username and password.

## Away from home

SMB only works on the NAS's network. From elsewhere, use the web drive through its public address, or set up a VPN to your home network (Cloudflare WARP with a private network route, Tailscale, or the router's VPN) and use the same `smb://` address.
