# Always-on hedger on Oracle Cloud (free)

The 5-minute collector notices a resting Kalshi fill minutes after it happens, which is too slow to hedge on Polymarket US.
`scripts/hedger.py` runs all the time on a small free server, checks Kalshi every ~5 seconds, and hedges within seconds.
**Right now it's practice-only.** It sends no orders, holds no API keys, and logs what fast hedging would have done to the
`hedger-data` branch (`fast_hedges.csv`). Live trading still needs everything in `trader.live_allowed()`.

## 1. Create the server (you, about 15 minutes)
1. Sign up at **cloud.oracle.com** → "Start for free". Oracle asks for a card to verify you, but Always Free resources never
   charge. Don't upgrade the account to Pay As You Go.
2. **Home region: US East (Ashburn).** It's closest to Kalshi and Polymarket, and you can't change it later.
3. Menu → Compute → Instances → **Create instance**:
   - Name: `jontay-hedger`
   - Image: **Canonical Ubuntu 24.04**
   - Shape: **VM.Standard.E2.1.Micro** (AMD, "Always Free-eligible"). The Ampere A1 shape is also free, but it often
     says "out of capacity".
   - Networking: keep the defaults (new virtual cloud network, public IPv4 address).
   - SSH keys: **Generate a key pair for me** → download the private key (keep it private; don't share it in chat).
   - Create. Wait for the status to show **Running** and copy the **Public IP address**.

## 2. Install the hedger (one command)
On your Mac, in Terminal (replace the key file name and the IP address):
```bash
chmod 600 ~/Downloads/ssh-key-*.key
ssh -i ~/Downloads/ssh-key-*.key ubuntu@PUBLIC_IP
```
Then on the server:
```bash
curl -fsSL https://raw.githubusercontent.com/nxtsgn/project-jontay-porter/main/deploy/oracle/setup.sh | bash
```
At the end it prints a key that starts `ssh-ed25519`.

## 3. Let it save its log (once)
GitHub → this repo → Settings → **Deploy keys** → Add deploy key. Title `oracle-hedger`, paste the printed key, tick **Allow
write access**. The key can only push to this one repo, and it never leaves the server.

## Checking on it
- From the server: `journalctl -u jontay-hedger -f` shows each sync and every fill it hedged. `sudo systemctl restart
  jontay-hedger` restarts it.
- It restarts itself after a crash or reboot, and picks up new code from `main` within 30 minutes.
- Oracle may reclaim an Always Free VM that's idle for 7 days (CPU under ~20%). This one polls all day, so that's unlikely. If
  it happens, re-run steps 1–2.
- If the repo goes private, the server's read-only `git pull` will need its own read key. Ask Claude to switch it.
