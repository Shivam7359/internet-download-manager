# Changelog

All notable changes to this project will be documented here.
Format: [Semantic Versioning](https://semver.org)

## [Unreleased]
- Changes not yet released go here

## [1.0.0] - 2026-03-28
### Added
- Initial public release
- Multi-chunk parallel downloading
- Chrome extension with smart download detection
- Secure pairing flow between extension and desktop
- System tray support with minimize on close
- Persistent token - no re-pairing after restart
- Batch download from pages
- Stream detection for video sites
- Download queue control from extension
- Smart file type detection with scoring algorithm
- Direct vs redirect button detection
- Bridge health monitoring and auto-restart

### Security
- Fernet encryption for stored credentials
- OS keyring integration for token storage
- Save path boundary enforcement
- Rate limiting with LRU eviction

Update format for future releases:

## [1.1.0] - YYYY-MM-DD
### Added        <- new features
### Changed      <- changes to existing features
### Fixed        <- bug fixes
### Removed      <- removed features
### Security     <- security fixes
### Performance  <- performance improvements