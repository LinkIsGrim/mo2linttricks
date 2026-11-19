package main

import "testing"

func TestMatchExe(t *testing.T) {
	tg := []string{"cyberpunk2077.exe"}
	cases := []struct {
		cmd  string
		want bool
	}{
		{`Z:\home\grim\.local\share\Steam\steamapps\common\Cyberpunk 2077\bin\x64\Cyberpunk2077.exe --launcher-skip -modded`, true},
		{"Z:\\games\\bin\\x64\\Cyberpunk2077.exe\x00--launcher-skip\x00", true},
		{`"Z:\games\Cyberpunk 2077\bin\x64\Cyberpunk2077.exe"`, true},
		{`c:\windows\system32\steam.exe /home/x/Cyberpunk 2077/bin/x64/Cyberpunk2077.exe`, false},
		{"/home/grim/mo2linttricks/mo2lt-gamemode\x00--target\x00Cyberpunk2077.exe\x00", false},
		{`Z:\games\bin\x64\Cyberpunk2077.exe.bak`, false},
		{`Z:\games\bin\x64\NotCyberpunk2077.exe`, false},
		{`Z:\home\grim\Games\mo2\ModOrganizer.exe`, false},
		{"Cyberpunk2077.exe\x00--launcher-skip\x00", true},
		{"xCyberpunk2077.exe", false},
	}
	for _, c := range cases {
		if _, got := matchExe([]byte(c.cmd), tg); got != c.want {
			t.Errorf("matchExe(%q) = %v, want %v", c.cmd, got, c.want)
		}
	}
}
