// Copyright 2026 Alibaba Group Holding Ltd.
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.

package lifecycle

import (
	"os"
	"path/filepath"
	"testing"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"
)

func TestLoadConfigMaterializesEnvironmentConfig(t *testing.T) {
	path := filepath.Join(t.TempDir(), "state", "lifecycle.toml")
	t.Setenv(ConfigPathEnv, path)
	t.Setenv(ConfigEnv, `{
  "version": 1,
  "preStart": {"command": ["sh", "-c", "echo ready"], "timeoutSeconds": 5},
  "periodic": [{"name": "sync", "schedule": "@every 5m", "command": ["sync"]}]
}`)

	cfg, err := LoadConfig()
	require.NoError(t, err)
	require.NotNil(t, cfg)
	assert.Equal(t, []string{"sh", "-c", "echo ready"}, cfg.PreStart.Command)
	require.FileExists(t, path)

	t.Setenv(ConfigEnv, "")
	reloaded, err := LoadConfig()
	require.NoError(t, err)
	require.NotNil(t, reloaded)
	assert.Equal(t, "sync", reloaded.Periodic[0].Name)
}

func TestLoadConfigEnvironmentOverridesPersistedConfig(t *testing.T) {
	path := filepath.Join(t.TempDir(), "lifecycle.toml")
	require.NoError(t, os.WriteFile(path, []byte(`version = 1
[preStart]
command = ["old"]
`), 0o600))
	t.Setenv(ConfigPathEnv, path)
	t.Setenv(ConfigEnv, `{"preStart":{"command":["new"]}}`)

	cfg, err := LoadConfig()
	require.NoError(t, err)
	require.NotNil(t, cfg)
	assert.Equal(t, []string{"new"}, cfg.PreStart.Command)

	t.Setenv(ConfigEnv, "")
	reloaded, err := LoadConfig()
	require.NoError(t, err)
	require.NotNil(t, reloaded)
	assert.Equal(t, []string{"new"}, reloaded.PreStart.Command)
}

func TestDecodeConfigRejectsDuplicatePeriodicNames(t *testing.T) {
	_, err := decodeConfig([]byte(`{
  "periodic": [
    {"name": "sync", "schedule": "@hourly", "command": ["true"]},
    {"name": "sync", "schedule": "@daily", "command": ["true"]}
  ]
}`))

	require.ErrorContains(t, err, `duplicate periodic hook name "sync"`)
}

func TestDecodeConfigRejectsInvalidPeriodicSchedule(t *testing.T) {
	_, err := decodeConfig([]byte(`{
  "periodic": [{"name": "sync", "schedule": "61 * * * *", "command": ["true"]}]
}`))

	require.ErrorContains(t, err, `periodic hook "sync" has invalid schedule`)
}

func TestDecodeConfigNormalizesPeriodicIdentityAndSchedule(t *testing.T) {
	cfg, err := decodeConfig([]byte(`{
  "periodic": [{"name": " sync ", "schedule": " @every 1m ", "command": ["true"]}]
}`))

	require.NoError(t, err)
	assert.Equal(t, "sync", cfg.Periodic[0].Name)
	assert.Equal(t, "@every 1m", cfg.Periodic[0].Schedule)
}

func TestDecodeConfigRejectsSubSecondEveryInterval(t *testing.T) {
	_, err := decodeConfig([]byte(`{
  "periodic": [{"name": "sync", "schedule": "@every 500ms", "command": ["true"]}]
}`))

	require.ErrorContains(t, err, `periodic hook "sync" @every interval must be a whole number of seconds`)
}

func TestDecodeConfigRejectsOverflowingTimeout(t *testing.T) {
	_, err := decodeConfig([]byte(`{
  "preStart": {"command": ["true"], "timeoutSeconds": 9223372037}
}`))

	require.ErrorContains(t, err, "timeoutSeconds is too large")
}

func TestLoadConfigRejectsInvalidPersistedConfig(t *testing.T) {
	path := filepath.Join(t.TempDir(), "lifecycle.toml")
	require.NoError(t, os.WriteFile(path, []byte("not valid TOML ="), 0o600))
	t.Setenv(ConfigPathEnv, path)
	t.Setenv(ConfigEnv, "")

	cfg, err := LoadConfig()
	require.ErrorContains(t, err, "invalid persisted lifecycle config")
	assert.Nil(t, cfg)
}

func TestLoadConfigReturnsNilWhenNotConfigured(t *testing.T) {
	t.Setenv(ConfigPathEnv, filepath.Join(t.TempDir(), "lifecycle.toml"))
	t.Setenv(ConfigEnv, "")

	cfg, err := LoadConfig()
	require.NoError(t, err)
	assert.Nil(t, cfg)
	_, statErr := os.Stat(os.Getenv(ConfigPathEnv))
	assert.ErrorIs(t, statErr, os.ErrNotExist)
}
