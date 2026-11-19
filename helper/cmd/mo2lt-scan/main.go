// mo2lt-scan parses TweakXL YAML files for the mo2linttricks MO2 plugin, out of process.
//
// MO2's Python plugins share one GIL with MO2's UI thread (game plugins are Python
// too), so parsing ~1000 YAML files in pure Python stalls MO2 even from a background
// thread. This helper does the parsing in a separate process instead. It is built
// as a Windows executable so it runs under Wine/Proton exactly like on Windows.
//
// Protocol: JSON on stdin {"roots": ["<mod folder>", ...]}, JSON on stdout
// {"files": [{"path": "...", "entries": [[key, value], ...], "error": "..."}]}.
// Only r6/tweaks/**/*.yaml|yml are read. Semantics mirror core/tweaks.py parse_yaml:
// plain value assignments are reported as "Record.property"; list operations
// (tags such as !append) and $type/$instances/$props are skipped. Numbers keep their
// original text (json.Number) so 1.0 stays a float on the Python side.
package main

import (
	"bufio"
	"encoding/json"
	"fmt"
	"io/fs"
	"os"
	"path/filepath"
	"slices"
	"sort"
	"strings"

	"gopkg.in/yaml.v3"
)

type request struct {
	Roots []string `json:"roots"`
}

type fileResult struct {
	Path    string   `json:"path"`
	Entries [][2]any `json:"entries"`
	Error   string   `json:"error,omitempty"`
}

func main() {
	var req request
	if err := json.NewDecoder(bufio.NewReader(os.Stdin)).Decode(&req); err != nil {
		fmt.Fprintln(os.Stderr, "bad request:", err)
		os.Exit(2)
	}
	out := struct {
		Files []fileResult `json:"files"`
	}{Files: []fileResult{}}
	for _, root := range req.Roots {
		tweaks := filepath.Join(root, "r6", "tweaks")
		var paths []string
		_ = filepath.WalkDir(tweaks, func(p string, d fs.DirEntry, err error) error {
			if err != nil {
				return nil
			}
			ext := strings.ToLower(filepath.Ext(p))
			if !d.IsDir() && (ext == ".yaml" || ext == ".yml") {
				paths = append(paths, p)
			}
			return nil
		})
		sort.Strings(paths)
		for _, p := range paths {
			out.Files = append(out.Files, parseFile(p))
		}
	}
	w := bufio.NewWriter(os.Stdout)
	enc := json.NewEncoder(w)
	enc.SetEscapeHTML(false)
	if err := enc.Encode(out); err != nil {
		fmt.Fprintln(os.Stderr, "encode:", err)
		os.Exit(1)
	}
	_ = w.Flush()
}

func parseFile(p string) (res fileResult) {
	res.Path = p
	res.Entries = [][2]any{}
	data, err := os.ReadFile(p)
	if err != nil {
		res.Error = err.Error()
		return
	}
	// TweakXL's yaml-cpp tolerates tabs (e.g. before trailing comments); yaml.v3 doesn't.
	text := strings.ReplaceAll(string(data), "\t", "    ")
	var doc yaml.Node
	if err := yaml.Unmarshal([]byte(text), &doc); err != nil {
		res.Error = err.Error()
		return
	}
	if len(doc.Content) == 0 || doc.Content[0].Kind != yaml.MappingNode {
		return
	}
	top := deref(doc.Content[0])
	// a key set twice in one file: the last one counts (as in PyYAML and TweakXL)
	index := map[string]int{}
	add := func(k string, v any) {
		if i, ok := index[k]; ok {
			res.Entries[i] = [2]any{k, v}
			return
		}
		index[k] = len(res.Entries)
		res.Entries = append(res.Entries, [2]any{k, v})
	}
	for i := 0; i+1 < len(top.Content); i += 2 {
		key, val := top.Content[i].Value, deref(top.Content[i+1])
		if val.Kind == yaml.MappingNode && !isOp(val) {
			for j := 0; j+1 < len(val.Content); j += 2 {
				prop, pv := val.Content[j].Value, deref(val.Content[j+1])
				if prop == "$type" || prop == "$instances" || prop == "$props" || hasOp(pv) {
					continue
				}
				add(key+"."+prop, toValue(pv))
			}
		} else if strings.Contains(key, ".") && !hasOp(val) {
			add(key, toValue(val))
		}
	}
	return
}

// deref follows YAML aliases (*anchor) to the node they point at.
func deref(n *yaml.Node) *yaml.Node {
	for n != nil && n.Kind == yaml.AliasNode && n.Alias != nil {
		n = n.Alias
	}
	return n
}

// isOp reports a node carrying a TweakXL operation tag (!append, !remove, ...).
func isOp(n *yaml.Node) bool {
	t := deref(n).Tag
	return strings.HasPrefix(t, "!") && !strings.HasPrefix(t, "!!")
}

// hasOp: the node is an op, or a sequence containing ops (list merge, not a replacement).
func hasOp(n *yaml.Node) bool {
	if isOp(n) {
		return true
	}
	n = deref(n)
	return n.Kind == yaml.SequenceNode && slices.ContainsFunc(n.Content, hasOp)
}

func toValue(n *yaml.Node) any {
	n = deref(n)
	var v any
	switch n.Kind {
	case yaml.ScalarNode:
		switch n.ShortTag() {
		case "!!int", "!!float":
			v = json.Number(normalizeNumber(n.Value))
		case "!!bool":
			var b bool
			_ = n.Decode(&b)
			v = b
		case "!!null":
			v = nil
		default:
			v = n.Value
		}
	case yaml.SequenceNode:
		items := make([]any, 0, len(n.Content))
		for _, c := range n.Content {
			items = append(items, toValue(c))
		}
		v = items
	case yaml.MappingNode:
		m := make(map[string]any, len(n.Content)/2)
		for i := 0; i+1 < len(n.Content); i += 2 {
			m[n.Content[i].Value] = toValue(n.Content[i+1])
		}
		v = m
	default:
		v = n.Value
	}
	if isOp(n) {
		return map[string]any{"$tag": n.Tag, "v": v}
	}
	return v
}

// normalizeNumber turns YAML number spellings into valid JSON numbers, keeping
// the decimal point (1.0 must stay a float for TweakDB's Float flats).
func normalizeNumber(s string) string {
	s = strings.ReplaceAll(s, "_", "")
	s = strings.TrimPrefix(s, "+")
	if strings.HasPrefix(s, ".") {
		s = "0" + s
	} else if strings.HasPrefix(s, "-.") {
		s = "-0" + s[1:]
	}
	if strings.HasSuffix(s, ".") {
		s += "0"
	}
	var f float64
	if _, err := fmt.Sscan(s, &f); err != nil {
		return "0"
	}
	return s
}
