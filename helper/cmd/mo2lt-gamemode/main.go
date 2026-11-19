// mo2lt-gamemode registers Wine game processes with Feral GameMode.
//
// It is started by the mo2linttricks MO2 plugin (from inside Wine/Proton, and
// usually inside Steam's pressure-vessel container) right before MO2 launches
// a game. It waits for a process whose Windows executable matches one of the
// --target names, registers it with GameMode over D-Bus, and exits once every
// matched process has exited.
//
// It is a static binary on purpose: inside the Steam runtime container the
// host's gamemoded/libgamemode may not be usable, but the session bus is.
package main

import (
	"bytes"
	"errors"
	"flag"
	"fmt"
	"io"
	"log"
	"os"
	"os/exec"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"time"

	"github.com/godbus/dbus/v5"
)

const (
	gmDest  = "com.feralinteractive.GameMode"
	gmPath  = "/com/feralinteractive/GameMode"
	gmIface = "com.feralinteractive.GameMode"
)

type targets []string

func (t *targets) String() string { return strings.Join(*t, ",") }
func (t *targets) Set(v string) error {
	for s := range strings.SplitSeq(v, ",") {
		if s = strings.TrimSpace(s); s != "" {
			*t = append(*t, strings.ToLower(s))
		}
	}
	return nil
}

func defaultLogPath() string {
	base := os.Getenv("XDG_STATE_HOME")
	if base == "" {
		home, _ := os.UserHomeDir()
		base = filepath.Join(home, ".local", "state")
	}
	return filepath.Join(base, "mo2linttricks", "gamemode.log")
}

func main() {
	var tgts targets
	flag.Var(&tgts, "target", "Windows executable name to register, e.g. Cyberpunk2077.exe (repeatable or comma separated)")
	wait := flag.Duration("wait", 3*time.Minute, "how long to wait for a target process to appear")
	grace := flag.Duration("grace", 8*time.Second, "keep watching this long after the last target exits (launcher -> game hand-offs)")
	poll := flag.Duration("poll", 2*time.Second, "process scan interval")
	logPath := flag.String("log", defaultLogPath(), "log file ('-' for stderr)")
	probe := flag.Bool("probe", false, "print environment diagnostics and GameMode status, then exit")
	flag.Parse()

	setupLog(*logPath)

	if *probe {
		os.Exit(runProbe())
	}
	if len(tgts) == 0 {
		log.Fatal("no --target given")
	}
	log.Printf("started pid=%d nspid=%q targets=%v", os.Getpid(), nsPid("self"), tgts)
	os.Exit(run(tgts, *wait, *grace, *poll))
}

func setupLog(path string) {
	log.SetFlags(log.Ldate | log.Ltime)
	if path == "-" {
		return
	}
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		return
	}
	f, err := os.OpenFile(path, os.O_CREATE|os.O_WRONLY|os.O_APPEND, 0o644)
	if err != nil {
		return
	}
	log.SetOutput(io.MultiWriter(f, os.Stderr))
}

// --- process matching -------------------------------------------------------

// matchExe reports whether cmdline belongs to a process running one of the
// target executables. Wine processes expose their Windows command line, e.g.
// `Z:\games\Cyberpunk 2077\bin\x64\Cyberpunk2077.exe --launcher-skip`. Only the
// first executable on the line counts, so wrappers like
// `c:\windows\system32\steam.exe /path/game.exe` or our own `--target x.exe`
// arguments never match.
func matchExe(cmdline []byte, tgts []string) (string, bool) {
	s := strings.ToLower(strings.TrimLeft(string(bytes.ReplaceAll(cmdline, []byte{0}, []byte{' '})), `"' `))
	for _, t := range tgts {
		for from := 0; ; {
			i := strings.Index(s[from:], t)
			if i < 0 {
				break
			}
			i += from
			from = i + 1
			if i > 0 && s[i-1] != '\\' && s[i-1] != '/' {
				continue
			}
			end := i + len(t)
			if end < len(s) && s[end] != ' ' && s[end] != '"' {
				continue
			}
			if strings.Contains(s[:i], ".exe") {
				continue
			}
			return t, true
		}
	}
	return "", false
}

func scan(tgts []string) map[int]string {
	found := map[int]string{}
	self := os.Getpid()
	entries, err := os.ReadDir("/proc")
	if err != nil {
		return found
	}
	for _, e := range entries {
		pid, err := strconv.Atoi(e.Name())
		if err != nil || pid == self {
			continue
		}
		cmd, err := os.ReadFile("/proc/" + e.Name() + "/cmdline")
		if err != nil || len(cmd) == 0 {
			continue
		}
		if t, ok := matchExe(cmd, tgts); ok {
			found[pid] = t
		}
	}
	return found
}

func alive(pid int) bool {
	_, err := os.Stat("/proc/" + strconv.Itoa(pid))
	return err == nil
}

// nsPid returns the NSpid line of /proc/<pid>/status: one value when the
// process lives in the root PID namespace, several when it is nested.
func nsPid(pid string) string {
	b, err := os.ReadFile("/proc/" + pid + "/status")
	if err != nil {
		return ""
	}
	for l := range strings.SplitSeq(string(b), "\n") {
		if v, ok := strings.CutPrefix(l, "NSpid:"); ok {
			return strings.Join(strings.Fields(v), " ")
		}
	}
	return ""
}

// --- GameMode ---------------------------------------------------------------

type gameMode struct {
	conn *dbus.Conn
	obj  dbus.BusObject
}

func connectGameMode() (*gameMode, error) {
	conn, err := dbus.ConnectSessionBus()
	if err != nil {
		return nil, err
	}
	return &gameMode{conn: conn, obj: conn.Object(gmDest, gmPath)}, nil
}

func (g *gameMode) callPID(method string, pid int) (int32, error) {
	var ret int32
	err := g.obj.Call(gmIface+"."+method, 0, int32(os.Getpid()), int32(pid)).Store(&ret)
	return ret, err
}

func (g *gameMode) register(pid int) error {
	ret, err := g.callPID("RegisterGameByPID", pid)
	if err != nil {
		return err
	}
	if ret != 0 {
		return fmt.Errorf("RegisterGameByPID returned %d", ret)
	}
	return nil
}

func (g *gameMode) unregister(pid int) {
	if ret, err := g.callPID("UnregisterGameByPID", pid); err != nil || ret != 0 {
		log.Printf("unregister pid %d: ret=%d err=%v", pid, ret, err)
	}
}

// gamemodedFallback uses the gamemoded client when the session bus is not
// reachable. Inside pressure-vessel the host binary lives under /run/host.
func gamemodedFallback(pid int) error {
	for _, p := range []string{"gamemoded", "/run/host/usr/bin/gamemoded", "/usr/bin/gamemoded"} {
		path, err := exec.LookPath(p)
		if err != nil {
			continue
		}
		out, err := exec.Command(path, "-r"+strconv.Itoa(pid)).CombinedOutput()
		log.Printf("fallback %s -r%d: %s", path, pid, strings.TrimSpace(string(out)))
		return err
	}
	return errors.New("no gamemoded binary found")
}

// --- main loop --------------------------------------------------------------

func run(tgts []string, wait, grace, poll time.Duration) int {
	gm, err := connectGameMode()
	if err != nil {
		log.Printf("session bus unavailable (%v); will try gamemoded binary", err)
	}

	registered := map[int]string{}
	deadline := time.Now().Add(wait)
	var lastSeen time.Time

	for {
		for pid, t := range scan(tgts) {
			if _, ok := registered[pid]; ok {
				continue
			}
			registered[pid] = t
			var err error
			if gm != nil {
				err = gm.register(pid)
			} else {
				err = gamemodedFallback(pid)
			}
			if err != nil {
				log.Printf("register %s pid=%d nspid=%q FAILED: %v", t, pid, nsPid(strconv.Itoa(pid)), err)
			} else {
				log.Printf("registered %s pid=%d nspid=%q", t, pid, nsPid(strconv.Itoa(pid)))
			}
		}

		anyAlive := false
		for pid := range registered {
			if alive(pid) {
				anyAlive = true
			} else {
				if gm != nil {
					gm.unregister(pid)
				}
				log.Printf("pid %d exited", pid)
				delete(registered, pid)
				lastSeen = time.Now()
			}
		}

		switch {
		case anyAlive:
			lastSeen = time.Now()
		case lastSeen.IsZero() && time.Now().After(deadline):
			log.Printf("no target appeared within %s, exiting", wait)
			return 0
		case !lastSeen.IsZero() && time.Since(lastSeen) > grace:
			log.Printf("all targets exited, done")
			return 0
		}
		time.Sleep(poll)
	}
}

func runProbe() int {
	log.Printf("pid=%d nspid=%q uid=%d", os.Getpid(), nsPid("self"), os.Getuid())
	log.Printf("DBUS_SESSION_BUS_ADDRESS=%q", os.Getenv("DBUS_SESSION_BUS_ADDRESS"))
	log.Printf("pressure-vessel=%v flatpak=%v", fileExists("/run/pressure-vessel"), fileExists("/.flatpak-info"))
	gm, err := connectGameMode()
	if err != nil {
		log.Printf("session bus: ERROR %v", err)
		return 1
	}
	var names []string
	_ = gm.conn.BusObject().Call("org.freedesktop.DBus.ListNames", 0).Store(&names)
	sort.Strings(names)
	has := false
	for _, n := range names {
		if n == gmDest {
			has = true
		}
	}
	log.Printf("session bus: ok, gamemode service present=%v", has)
	ret, err := gm.callPID("QueryStatusByPID", os.Getpid())
	log.Printf("QueryStatusByPID(self) = %d err=%v", ret, err)
	return 0
}

func fileExists(p string) bool { _, err := os.Stat(p); return err == nil }
