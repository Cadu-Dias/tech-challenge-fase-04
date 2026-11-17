package main

import (
	"errors"
	"os"
)

const faultMarkerPath = "/tmp/togglemaster-auth-fault"

func injectFault(enabled bool, path string) error {
	if !enabled {
		return errors.New("injeção de falha desabilitada")
	}
	file, err := os.OpenFile(path, os.O_WRONLY|os.O_CREATE|os.O_EXCL, 0600)
	if errors.Is(err, os.ErrExist) {
		return nil
	}
	if err != nil {
		return err
	}
	return file.Close()
}

func (a *App) faultActive() (bool, error) {
	if !a.FaultEnabled {
		return false, nil
	}
	_, err := os.Stat(a.FaultMarker)
	if errors.Is(err, os.ErrNotExist) {
		return false, nil
	}
	if err != nil {
		return false, err
	}
	return true, nil
}
