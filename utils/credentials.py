# IDM v2.0 — credentials.py — audited 2026-03-28
"""
IDM Utilities — Secure Credential Storage
===========================================
Platform-aware credential management using OS keyring or encrypted fallback.

Features:
    • OS keyring integration (Windows Credential Manager, macOS Keychain, Linux Secret Service)
    • Encrypted fallback storage in ~/.idm/credentials
    • Simple API: store/retrieve auth tokens securely
    • Automatic cleanup and key rotation helpers

Usage::

    from utils.credentials import CredentialStore
    
    store = CredentialStore()
    store.store("api_token", "secret_value")
    token = store.retrieve("api_token")
    store.delete("api_token")
"""

import os
import logging
from pathlib import Path
from typing import Optional
import json
from cryptography.fernet import Fernet, InvalidToken

log = logging.getLogger("idm.utils.credentials")

# Try to import keyring
try:
    import keyring
    from keyring.errors import keyring_errors
    HAS_KEYRING = True
except ImportError:
    HAS_KEYRING = False
    log.warning(
        "keyring library not installed. Credentials will use encrypted local storage. "
        "Install keyring for OS-level credential storage: pip install keyring"
    )


class CredentialStore:
    """
    Secure credential storage with OS keyring fallback.
    
    Tries to use OS keyring first (Credential Manager on Windows, Keychain on macOS,
    Secret Service on Linux). Falls back to encrypted local file storage if keyring
    is unavailable.
    """
    
    SERVICE_NAME = "IDM-InternetDownloadManager"
    LOCAL_KEY_ALIAS = "__local_fernet_key__"
    CREDENTIALS_DIR = Path.home() / ".idm" / "credentials"
    CREDS_FILE = CREDENTIALS_DIR / "secrets.json"
    KEY_FILE = CREDENTIALS_DIR / "fernet.key"
    
    def __init__(self, prefer_keyring: bool = True):
        """
        Args:
            prefer_keyring: If True, try OS keyring first before fallback.
        """
        self.prefer_keyring = prefer_keyring and HAS_KEYRING
        self._keyring_available = False
        
        if self.prefer_keyring:
            try:
                # Test keyring availability
                keyring.get_keyring()
                self._keyring_available = True
                log.debug("OS keyring detected and available")
            except Exception as e:
                log.debug("Keyring unavailable, using encrypted local storage: %s", e)
                self._keyring_available = False
        
        # Ensure local storage directory exists
        self.CREDENTIALS_DIR.mkdir(parents=True, exist_ok=True)
        # Restrict permissions to owner only
        try:
            os.chmod(self.CREDENTIALS_DIR, 0o700)
        except Exception:
            pass
    
    def store(self, key: str, value: str) -> bool:
        """
        Store a credential securely.
        
        Args:
            key: Credential identifier (e.g., "api_token", "proxy_password")
            value: Secret value to store
        
        Returns:
            True if stored successfully, False otherwise.
        """
        if not key or not value:
            return False
        
        try:
            if self._keyring_available:
                try:
                    keyring.set_password(self.SERVICE_NAME, key, value)
                    log.debug("Stored credential '%s' in OS keyring", key)
                    return True
                except Exception as e:
                    log.warning("Failed to store in keyring, using local storage: %s", e)
            
            # Fallback: encrypted local storage
            return self._store_local(key, value)
        
        except Exception as e:
            log.error("Failed to store credential '%s': %s", key, e)
            return False
    
    def retrieve(self, key: str) -> Optional[str]:
        """
        Retrieve a stored credential.
        
        Args:
            key: Credential identifier
        
        Returns:
            The stored secret value, or None if not found/failed to retrieve.
        """
        if not key:
            return None
        
        try:
            if self._keyring_available:
                try:
                    value = keyring.get_password(self.SERVICE_NAME, key)
                    if value:
                        log.debug("Retrieved credential '%s' from OS keyring", key)
                        return value
                except Exception as e:
                    log.debug("Failed to retrieve from keyring: %s", e)
            
            # Fallback: try local storage
            return self._retrieve_local(key)
        
        except Exception as e:
            log.error("Failed to retrieve credential '%s': %s", key, e)
            return None
    
    def delete(self, key: str) -> bool:
        """
        Delete a stored credential.
        
        Args:
            key: Credential identifier
        
        Returns:
            True if deleted successfully, False otherwise.
        """
        if not key:
            return False
        
        try:
            if self._keyring_available:
                try:
                    keyring.delete_password(self.SERVICE_NAME, key)
                    log.debug("Deleted credential '%s' from OS keyring", key)
                    return True
                except keyring_errors.PasswordDeleteError:
                    # Key doesn't exist in keyring, try local
                    pass
                except Exception as e:
                    log.warning("Failed to delete from keyring: %s", e)
            
            # Fallback: try local storage
            return self._delete_local(key)
        
        except Exception as e:
            log.error("Failed to delete credential '%s': %s", key, e)
            return False
    
    def _store_local(self, key: str, value: str) -> bool:
        """Store credential in local Fernet-encrypted file."""
        try:
            data = {}
            if self.CREDS_FILE.exists():
                try:
                    with open(self.CREDS_FILE, 'r') as f:
                        data = json.load(f)
                except Exception:
                    pass

            fernet = self._get_fernet()
            if fernet is None:
                log.error("Local credential encryption key unavailable")
                return False

            token = fernet.encrypt(value.encode("utf-8")).decode("ascii")
            data[key] = {
                "v": 1,
                "alg": "fernet",
                # Fernet payload includes IV/nonce + ciphertext + auth tag.
                "ciphertext": token,
            }
            
            with open(self.CREDS_FILE, 'w') as f:
                json.dump(data, f)
            
            # Restrict file permissions
            try:
                os.chmod(self.CREDS_FILE, 0o600)
            except Exception:
                pass
            
            log.debug("Stored credential '%s' in local encrypted storage", key)
            return True
        
        except Exception as e:
            log.error("Failed to store credential locally: %s", e)
            return False
    
    def _retrieve_local(self, key: str) -> Optional[str]:
        """Retrieve credential from local Fernet-encrypted storage."""
        try:
            if not self.CREDS_FILE.exists():
                return None
            
            with open(self.CREDS_FILE, 'r') as f:
                data = json.load(f)
            
            if key not in data:
                return None

            entry = data[key]
            if isinstance(entry, str):
                # Legacy reversible format is intentionally no longer supported.
                log.warning("Legacy credential format detected for '%s'; ignoring insecure value", key)
                return None

            ciphertext = str(entry.get("ciphertext", "")).strip() if isinstance(entry, dict) else ""
            if not ciphertext:
                return None

            fernet = self._get_fernet()
            if fernet is None:
                return None

            decoded = fernet.decrypt(ciphertext.encode("ascii")).decode("utf-8")
            
            log.debug("Retrieved credential '%s' from local storage", key)
            return decoded

        except InvalidToken:
            log.error("Credential '%s' failed authentication/decryption", key)
            return None
        
        except Exception as e:
            log.error("Failed to retrieve credential from local storage: %s", e)
            return None
    
    def _delete_local(self, key: str) -> bool:
        """Delete credential from local encrypted storage."""
        try:
            if not self.CREDS_FILE.exists():
                return False
            
            with open(self.CREDS_FILE, 'r') as f:
                data = json.load(f)
            
            if key not in data:
                return False
            
            del data[key]
            
            with open(self.CREDS_FILE, 'w') as f:
                json.dump(data, f)
            
            log.debug("Deleted credential '%s' from local storage", key)
            return True
        
        except Exception as e:
            log.error("Failed to delete credential from local storage: %s", e)
            return False

    def _get_fernet(self) -> Optional[Fernet]:
        """Get Fernet instance backed by keyring or local key file."""
        key = self._load_or_create_local_key()
        if not key:
            return None
        try:
            return Fernet(key)
        except Exception as exc:
            log.error("Invalid local encryption key material: %s", exc)
            return None

    def _load_or_create_local_key(self) -> Optional[bytes]:
        """Load local encryption key from keyring first, then key file fallback."""
        # Prefer OS keyring for key material storage when available.
        if self._keyring_available:
            try:
                encoded = keyring.get_password(self.SERVICE_NAME, self.LOCAL_KEY_ALIAS)
                if encoded:
                    return encoded.encode("ascii")
            except Exception as exc:
                log.debug("Could not read local crypto key from keyring: %s", exc)

        try:
            if self.KEY_FILE.exists():
                raw = self.KEY_FILE.read_bytes().strip()
                if raw:
                    return raw
        except Exception as exc:
            log.warning("Failed reading local key file: %s", exc)

        try:
            key = Fernet.generate_key()

            stored_in_keyring = False
            if self._keyring_available:
                try:
                    keyring.set_password(
                        self.SERVICE_NAME,
                        self.LOCAL_KEY_ALIAS,
                        key.decode("ascii"),
                    )
                    stored_in_keyring = True
                except Exception as exc:
                    log.warning("Failed persisting local crypto key to keyring: %s", exc)

            if not stored_in_keyring:
                self.KEY_FILE.write_bytes(key)
                try:
                    os.chmod(self.KEY_FILE, 0o600)
                except Exception:
                    pass

            return key
        except Exception as exc:
            log.error("Failed creating local encryption key: %s", exc)
            return None

    def encrypt_local_value(self, plaintext: str) -> Optional[str]:
        """Encrypt plaintext with local Fernet key and return token."""
        fernet = self._get_fernet()
        if fernet is None:
            return None
        try:
            return fernet.encrypt(plaintext.encode("utf-8")).decode("ascii")
        except Exception:
            return None

    def decrypt_local_value(self, token: str) -> Optional[str]:
        """Decrypt local Fernet token and return plaintext."""
        fernet = self._get_fernet()
        if fernet is None:
            return None
        try:
            return fernet.decrypt(token.encode("ascii")).decode("utf-8")
        except Exception:
            return None


# Global instance
_store: Optional[CredentialStore] = None


def get_credential_store() -> CredentialStore:
    """Get or create the global credential store instance."""
    global _store
    if _store is None:
        _store = CredentialStore()
    return _store


def encrypt_secret(plaintext: str) -> Optional[str]:
    """Encrypt a sensitive value using the shared local credential key."""
    return get_credential_store().encrypt_local_value(plaintext)


def decrypt_secret(token: str) -> Optional[str]:
    """Decrypt a sensitive value previously encrypted with encrypt_secret()."""
    return get_credential_store().decrypt_local_value(token)
