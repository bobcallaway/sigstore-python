# Copyright 2022 The Sigstore Authors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import base64
import io
import secrets

import pretend
import pytest
from sigstore_protobuf_specs.dev.sigstore.common.v1 import HashAlgorithm

import sigstore.oidc
from sigstore._internal.keyring import KeyringError, KeyringLookupError
from sigstore._internal.sct import InvalidSCTError, InvalidSCTKeyError
from sigstore._utils import sha256_streaming
from sigstore.hashes import Hashed
from sigstore.sign import SigningContext
from sigstore.verify.models import VerificationMaterials
from sigstore.verify.policy import UnsafeNoOp
from sigstore.verify.verifier import Verifier


class TestSigningContext:
    @pytest.mark.online
    def test_production(self):
        assert SigningContext.production() is not None

    def test_staging(self, mock_staging_tuf):
        assert SigningContext.staging() is not None


@pytest.mark.online
@pytest.mark.ambient_oidc
def test_sign_rekor_entry_consistent(signer_and_ident):
    ctx, identity = signer_and_ident

    # NOTE: The actual signer instance is produced lazily, so that parameter
    # expansion doesn't fail in offline tests.
    ctx: SigningContext = ctx()
    assert identity is not None

    payload = io.BytesIO(secrets.token_bytes(32))
    with ctx.signer(identity) as signer:
        expected_entry = signer.sign(payload).verification_material.tlog_entries[0]

    actual_entry = ctx._rekor.log.entries.get(log_index=expected_entry.log_index)

    assert expected_entry.canonicalized_body == base64.b64decode(actual_entry.body)
    assert expected_entry.integrated_time == actual_entry.integrated_time
    assert expected_entry.log_id.key_id == bytes.fromhex(actual_entry.log_id)
    assert expected_entry.log_index == actual_entry.log_index


@pytest.mark.online
@pytest.mark.ambient_oidc
def test_sct_verify_keyring_lookup_error(signer_and_ident, monkeypatch):
    ctx, identity = signer_and_ident

    # a signer whose keyring always fails to lookup a given key.
    ctx: SigningContext = ctx()
    ctx._rekor._ct_keyring = pretend.stub(verify=pretend.raiser(KeyringLookupError))
    assert identity is not None

    payload = io.BytesIO(secrets.token_bytes(32))

    with pytest.raises(
        InvalidSCTError,
    ) as excinfo:
        with ctx.signer(identity) as signer:
            signer.sign(payload)

    # The exception subclass is the one we expect.
    assert isinstance(excinfo.value, InvalidSCTKeyError)


@pytest.mark.online
@pytest.mark.ambient_oidc
def test_sct_verify_keyring_error(signer_and_ident, monkeypatch):
    ctx, identity = signer_and_ident

    # a signer whose keyring throws an internal error.
    ctx: SigningContext = ctx()
    ctx._rekor._ct_keyring = pretend.stub(verify=pretend.raiser(KeyringError))
    assert identity is not None

    payload = io.BytesIO(secrets.token_bytes(32))

    with pytest.raises(InvalidSCTError):
        with ctx.signer(identity) as signer:
            signer.sign(payload)


@pytest.mark.online
@pytest.mark.ambient_oidc
def test_identity_proof_claim_lookup(signer_and_ident, monkeypatch):
    ctx, identity = signer_and_ident

    ctx: SigningContext = ctx()
    assert identity is not None

    # clear out the known issuers, forcing the `Identity`'s  `proof_claim` to be looked up.
    monkeypatch.setattr(sigstore.oidc, "_KNOWN_OIDC_ISSUERS", {})

    payload = io.BytesIO(secrets.token_bytes(32))

    with ctx.signer(identity) as signer:
        expected_entry = signer.sign(payload).verification_material.tlog_entries[0]
    actual_entry = ctx._rekor.log.entries.get(log_index=expected_entry.log_index)

    assert expected_entry.canonicalized_body == base64.b64decode(actual_entry.body)
    assert expected_entry.integrated_time == actual_entry.integrated_time
    assert expected_entry.log_id.key_id == bytes.fromhex(actual_entry.log_id)
    assert expected_entry.log_index == actual_entry.log_index


@pytest.mark.online
@pytest.mark.ambient_oidc
def test_sign_prehashed(staging):
    sign_ctx, verifier, identity = staging

    sign_ctx: SigningContext = sign_ctx()
    verifier: Verifier = verifier()

    input_ = io.BytesIO(secrets.token_bytes(32))
    hashed = Hashed(digest=sha256_streaming(input_), algorithm=HashAlgorithm.SHA2_256)

    with sign_ctx.signer(identity) as signer:
        bundle = signer.sign(hashed)

    assert bundle.message_signature.message_digest.algorithm == hashed.algorithm
    assert bundle.message_signature.message_digest.digest == hashed.digest

    input_.seek(0)
    materials = VerificationMaterials.from_bundle(
        input_=input_, bundle=bundle, offline=False
    )
    verifier.verify(materials=materials, policy=UnsafeNoOp())
