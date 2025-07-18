# BoardHog

Monitor SpiNNaker2 board usage with traffic light indicators.

## Status Indicators
🟢 Free • 🟡 < 1min • 🟠 1-5min • 🔴 > 5min

## Installation
```bash
chmod +x ~/boardhog/boardhog.py
mkdir -p ~/.local/bin
ln -sf ~/boardhog/boardhog.py ~/.local/bin/boardhog
echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.bashrc && source ~/.bashrc
```

## Usage
```bash
boardhog                    # Check current status
watch -n .5 boardhog        # Real-time monitoring
boardhog | grep username    # Filter by user
```

## Output
```
Board Status

1.53 🟢
2.52 🟠 alice
3.24 🟢
4.xx 🟢
```

**Boards monitored**: 1.53, 2.52, 3.24, 4.xx (placeholder) / 4.* (when in use)

**Requirements**: Python 3.6+, access to `/tmp/s2*_lock*` files
