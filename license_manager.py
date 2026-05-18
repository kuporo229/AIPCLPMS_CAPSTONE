import os
import json
import logging
import tempfile
import requests
from datetime import datetime, timezone, timedelta
from functools import wraps
from flask import abort

logger = logging.getLogger(__name__)

class LicenseManager:
    def __init__(self, state_file="license_state.json", api_key=None):
        self._api_key = api_key
        self.base_url = "https://licensing.ludom.app"
        self.state_file = state_file
        self.default_check_interval_seconds = 900
        self.default_force_check_interval_seconds = 60
        self.default_verify_timeout_seconds = 2
        self.default_activate_timeout_seconds = 15

    @property
    def api_key(self):
        """Always return the most current key from environment or stored fallback."""
        return os.environ.get('LOUIS_LICENSE_API') or self._api_key

    def set_api_key(self, api_key):
        """Update the stored API key in memory."""
        if api_key:
            self._api_key = api_key
            os.environ['LOUIS_LICENSE_API'] = api_key


    @api_key.setter
    def api_key(self, value):
        self._api_key = value

    def _get_headers(self):
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json"
        }

    def _get_env_int(self, key, default_value):
        raw_val = os.environ.get(key)
        if raw_val is None:
            return default_value
        try:
            parsed = int(raw_val)
            return parsed if parsed > 0 else default_value
        except (TypeError, ValueError):
            logger.warning(f"Invalid {key} value: {raw_val!r}. Using default {default_value}.")
            return default_value

    def _is_license_enforcement_enabled(self):
        raw_val = os.environ.get("LOUIS_LICENSE_ENFORCEMENT_ENABLED")
        if raw_val is None:
            return True
        normalized = raw_val.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
        logger.warning(
            "Invalid LOUIS_LICENSE_ENFORCEMENT_ENABLED value: %r. Using default enabled state.",
            raw_val,
        )
        return True

    def _parse_date(self, dt_val):
        try:
            if not dt_val:
                return None
            if isinstance(dt_val, (int, float)):
                return datetime.fromtimestamp(dt_val, tz=timezone.utc)
            if isinstance(dt_val, str):
                if dt_val.isdigit():
                    return datetime.fromtimestamp(int(dt_val), tz=timezone.utc)
                clean_date = dt_val.replace('Z', '+00:00')
                return datetime.fromisoformat(clean_date)
            return None
        except Exception:
            return None

    def _write_state(self, state):
        state_dir = os.path.dirname(os.path.abspath(self.state_file)) or "."
        os.makedirs(state_dir, exist_ok=True)
        fd, temp_path = tempfile.mkstemp(prefix=".license_state.", suffix=".tmp", dir=state_dir)
        try:
            with os.fdopen(fd, "w") as temp_file:
                json.dump(state, temp_file)
                temp_file.flush()
                os.fsync(temp_file.fileno())
            os.replace(temp_path, self.state_file)
        except Exception:
            try:
                os.unlink(temp_path)
            except OSError:
                pass
            raise

    def _load_state(self):
        with open(self.state_file, "r") as f:
            return json.load(f)

    def _clear_invalid_state(self, reason):
        logger.error(f"Failed to read local license state: {reason}")
        try:
            os.remove(self.state_file)
        except OSError:
            pass

    def activate_license(self, license_key, domain, product="AIPCLPMS"):
        url = f"{self.base_url}/api/activate"
        payload = {"license_key": license_key, "domain": domain, "product": product}
        
        try:
            activate_timeout = self._get_env_int(
                "LOUIS_LICENSE_ACTIVATE_TIMEOUT_SECONDS",
                self.default_activate_timeout_seconds
            )
            resp = requests.post(url, headers=self._get_headers(), json=payload, timeout=activate_timeout)
            resp.raise_for_status()
            data = resp.json()
            
            # Persist the state if activation is successful
            if data.get("success") is True or data.get("status") == "success":
                from datetime import datetime, timezone, timedelta
                license_id = data.get("license", {}).get("id") or data.get("license_id")
                
                # Ensure we have some expiration date, default to 30 days if not provided
                expires_at = data.get("license", {}).get("expires_at") or data.get("expires_at")
                if not expires_at:
                    expires_at = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()

                state = {
                    "license_id": license_id,
                    "token": data.get("token"),
                    "domain": domain,
                    "expires_at": expires_at,
                    "last_checked": datetime.now(timezone.utc).isoformat()
                }
                self._write_state(state)
            return data
            
        except requests.exceptions.RequestException as e:
            logger.error(f"License activation request failed: {e}")
            return {"status": "error", "message": str(e)}
        except ValueError as e:
            logger.error(str(e))
            return {"status": "error", "message": str(e)}

    def verify_license(self, license_id, domain, token):
        url = f"{self.base_url}/api/verify"
        payload = {"license_id": license_id, "domain": domain, "token": token}
        
        try:
            verify_timeout = self._get_env_int(
                "LOUIS_LICENSE_VERIFY_TIMEOUT_SECONDS",
                self.default_verify_timeout_seconds
            )
            resp = requests.post(url, headers=self._get_headers(), json=payload, timeout=verify_timeout)
            
            # If the server explicitly says it's revoked or not found, it's invalid.
            if resp.status_code in [403, 404]:
                return {"valid": False, "reason": "revoked" if resp.status_code == 403 else "not_found"}
                
            resp.raise_for_status()
            return resp.json()
            
        except requests.exceptions.HTTPError as e:
            # For other 4xx errors, we should probably treat them as invalid.
            if e.response is not None and 400 <= e.response.status_code < 500:
                logger.error(f"License verification failed with client error: {e}")
                return {"valid": False, "status": "error", "message": str(e)}
            
            # For 5xx errors, we treat it as a temporary network/server issue.
            logger.error(f"License server error (5xx): {e}")
            return {"valid": None, "status": "error", "message": str(e)}
            
        except requests.exceptions.RequestException as e:
            # DNS issues, timeouts, connection refused, etc.
            logger.error(f"License verification network error: {e}")
            return {"valid": None, "status": "error", "message": str(e)}
        except ValueError as e:
            logger.error(str(e))
            return {"valid": False, "status": "error", "message": str(e)}

    def get_full_status(self):
        if not self._is_license_enforcement_enabled():
            return {
                "valid": True,
                "bypassed": True,
                "enforcement_enabled": False,
                "message": "License enforcement disabled via environment.",
            }
        if not os.path.exists(self.state_file):
            return {"valid": False, "message": "No license state file found."}
            
        try:
            state = self._load_state()
                
            license_id = state.get("license_id")
            token = state.get("token")
            domain = state.get("domain")
            
            if not all([license_id, token, domain]):
                return {"valid": False, "message": "License state file is incomplete."}
                
            result = self.verify_license(license_id, domain, token)
            
            # If the result is explicitly invalid, we should clear the local state to force a re-activation
            if result.get("valid") is False:
                try: os.remove(self.state_file)
                except: pass
                return result

            # If it's a network error (None), we can return the last known good state from the file
            if result.get("valid") is None:
                # Add a flag so the UI can show "Cached/Offline"
                state["is_cached"] = True
                state["valid"] = True
                return state

            is_valid = result.get("valid", result.get("status") in ["valid", "success"])
            if is_valid:
                state["expires_at"] = result.get("license", {}).get("expires_at") or result.get("expires_at") or state.get("expires_at")
                state["last_checked"] = datetime.now(timezone.utc).isoformat()
                self._write_state(state)
                    
            return result
            
        except (json.JSONDecodeError, IOError) as e:
            self._clear_invalid_state(e)
            return {"valid": False, "message": f"Error reading license state: {str(e)}"}

    def check_local_license(self, force_check=False):
        if not self._is_license_enforcement_enabled():
            return True
        if not os.path.exists(self.state_file):
            return False
            
        try:
            state = self._load_state()
                
            license_id = state.get("license_id")
            token = state.get("token")
            domain = state.get("domain")
            expires_at = state.get("expires_at")
            last_checked = state.get("last_checked")
            
            if not all([license_id, token, domain]):
                return False
                
            current_time = datetime.now(timezone.utc)
            expiry_date = self._parse_date(expires_at)
            last_date = self._parse_date(last_checked)
            local_unexpired = not expiry_date or current_time <= expiry_date
            
            if local_unexpired:
                interval_default = (
                    self.default_force_check_interval_seconds
                    if force_check else self.default_check_interval_seconds
                )
                interval_seconds = self._get_env_int(
                    "LOUIS_LICENSE_CHECK_INTERVAL_SECONDS",
                    interval_default
                )
                if last_date and current_time < last_date + timedelta(seconds=interval_seconds):
                    return True

            # If local validity is still good but API key is temporarily unavailable,
            # avoid blocking requests and trust local cache until next retry window.
            if local_unexpired and not self.api_key:
                logger.warning("License API key is missing; using cached local license state.")
                return True
                
            result = self.verify_license(license_id, domain, token)
            
            # If the server explicitly says INVALID (not a network error), 
            # we MUST clear the state and return False.
            if result.get("valid") is False:
                logger.warning(f"License {license_id} is no longer valid. Clearing local state.")
                try: os.remove(self.state_file)
                except: pass
                return False

            # If network error (valid is None), trust the local state if not expired.
            if result.get("valid") is None:
                fallback_valid = (current_time < expiry_date) if expiry_date else True
                if fallback_valid:
                    # Record the retry point to avoid retrying remote checks on every request
                    # when the licensing service is temporarily unreachable.
                    state["last_checked"] = current_time.isoformat()
                    try:
                        self._write_state(state)
                    except Exception:
                        pass
                return fallback_valid

            is_valid = result.get("valid", result.get("status") in ["valid", "success"])
            
            if is_valid:
                state["expires_at"] = result.get("license", {}).get("expires_at") or result.get("expires_at") or state.get("expires_at")
                state["last_checked"] = current_time.isoformat()
                self._write_state(state)
            else:
                # Explicitly invalid according to server
                try: os.remove(self.state_file)
                except: pass
                    
            return is_valid
            
        except (json.JSONDecodeError, IOError) as e:
            self._clear_invalid_state(e)
            return False

# Initialize a global instance for easy import
license_manager = LicenseManager()

def require_license(f):
    """
    Flask decorator that returns a 403 error if the local license is invalid or missing.
    """
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not license_manager.check_local_license():
            abort(403, description="Invalid or missing license")
        return f(*args, **kwargs)
    return decorated_function
