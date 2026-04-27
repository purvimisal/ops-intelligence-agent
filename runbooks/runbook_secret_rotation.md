# Runbook: Secret Rotation Failure

## Overview
Secret rotation failures occur when credentials (API keys, DB passwords, TLS certificates, service account tokens) are rotated but services are not updated to use the new credentials, or when the rotation process itself fails partway through, leaving services in an inconsistent state.

## Symptoms
- Sudden authentication failures across one or more services after a credential rotation event
- `error_rate` spike on services that depend on the rotated credential
- Logs showing: `authentication failed`, `invalid API key`, `certificate expired`, `token rejected`
- Services that were healthy immediately before rotation are now failing
- Multiple services failing simultaneously (indicates shared credential or shared rotation event)
- `cpu_pct` normal (not a compute issue — pure authentication failure)

## Root Cause Patterns
1. **Incomplete rotation**: New secret was rotated in the secret store but not all services that use it were restarted or reloaded to pick up the new value.
2. **Rotation overlap gap**: Old credential was revoked before new credential was fully propagated to all consumers.
3. **Secret version mismatch**: Multiple versions of a secret active simultaneously; service is pinned to old version that was deactivated.
4. **Certificate rotation timing**: TLS certificate deployed to server before clients trusted the new CA, or old certificate revoked before new certificate propagated.
5. **Rotation automation failure**: The rotation job ran partially — updated secret store but failed before updating the service or Kubernetes secret.
6. **Hard-coded credentials**: A service has a credential baked into a Docker image or config file rather than read from the secret store at runtime.

## Immediate Actions
1. Identify which credential was rotated and which services depend on it:
   ```bash
   # Check recent changes in your secret manager (e.g., AWS Secrets Manager, Vault)
   aws secretsmanager list-secret-version-ids --secret-id <name>
   vault kv metadata get secret/<path>
   ```
2. Verify all services that use the rotated secret have been updated and restarted:
   ```bash
   kubectl get pods -l <label> -o jsonpath='{.items[*].metadata.name}'
   kubectl rollout restart deployment/<service>
   ```
3. Check Kubernetes secrets are updated:
   ```bash
   kubectl get secret <secret-name> -o jsonpath='{.data.<key>}' | base64 -d
   ```
4. If rotation is partially complete and old credential is deactivated: **reactivate old credential immediately** to restore service, then redo rotation correctly with proper sequencing.
5. For certificate issues: check expiry and SANs on deployed cert:
   ```bash
   openssl s_client -connect <service>:443 2>/dev/null | openssl x509 -noout -text | grep -A2 "Validity\|DNS:"
   ```
6. After restoring service: audit for any hardcoded credentials in images or config:
   ```bash
   grep -r "APIKEY\|password\|secret\|token" --include="*.yaml" --include="*.json" k8s/
   ```

## Escalation Thresholds
- Multiple services failing simultaneously due to rotation → P1, coordinate immediate rollback of rotation
- Hard-coded credential discovered in source code → security incident, revoke credential immediately and rotate

## Correct Rotation Sequence
1. Generate new credential in secret store.
2. Update all services to reference new credential (Kubernetes secret, env injection).
3. Perform rolling restart of all dependent services.
4. Verify all services are using new credential (check auth success rate).
5. Only then: revoke old credential.
6. Document rotation in runbook and update rotation schedule.

## Prevention
- Never hard-code credentials; use secret store injection (AWS Secrets Manager sidecar, Vault agent, External Secrets Operator).
- Automate rotation using a two-phase process with validation between phases.
- Implement credential health checks as readiness probe or startup probe on each service.
- Overlap old and new credentials for a grace period (keep both active during transition).
- Alert on authentication failures immediately after any rotation event.
- Maintain a credential dependency map: know which services use which credentials.

## Related Runbooks
- runbook_deployment_regression.md (if rotation was done as part of a deployment)
- runbook_cascade_5xx.md (if auth failures are cascading across services)
