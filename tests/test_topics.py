"""Tests for topic resolution functions."""
from __future__ import annotations

import pytest

from bridge.topics import resolve_topic_template, get_topic, sanitize_client_id, get_broker_config
from tests.fakes import make_test_state, make_config


class TestResolveTopicTemplate:
    def test_replaces_iata(self):
        state = make_test_state(repeater_pub_key="AA" * 32)
        result = resolve_topic_template(state, "meshcore/{IATA}/test")
        assert result == "meshcore/TST/test"

    def test_replaces_pubkey(self):
        state = make_test_state(repeater_pub_key="AA" * 32)
        result = resolve_topic_template(state, "meshcore/test/{PUBLIC_KEY}")
        assert result == f"meshcore/test/{'AA' * 32}"

    def test_uses_broker_iata(self):
        config = make_config()
        config['broker'][0]['topics'] = {'iata': 'BRK'}
        state = make_test_state(config=config, repeater_pub_key="AA" * 32)
        result = resolve_topic_template(state, "meshcore/{IATA}/test", broker_idx=0)
        assert result == "meshcore/BRK/test"

    def test_falls_back_to_global_iata(self):
        state = make_test_state(repeater_pub_key="AA" * 32)
        result = resolve_topic_template(state, "meshcore/{IATA}/test", broker_idx=0)
        assert result == "meshcore/TST/test"

    def test_unknown_pubkey(self):
        state = make_test_state(repeater_pub_key=None)
        result = resolve_topic_template(state, "meshcore/{PUBLIC_KEY}/test")
        assert result == "meshcore/UNKNOWN/test"

    def test_empty_template(self):
        state = make_test_state()
        result = resolve_topic_template(state, "")
        assert result == ""


class TestGetTopic:
    def test_global_topic(self):
        state = make_test_state(repeater_pub_key="AA" * 32)
        result = get_topic(state, "packets")
        assert result == f"meshcore/TST/{'AA' * 32}/packets"

    def test_broker_override(self):
        config = make_config()
        config['broker'][0]['topics'] = {'packets': 'custom/{IATA}/packets'}
        state = make_test_state(config=config, repeater_pub_key="AA" * 32)
        result = get_topic(state, "packets", broker_idx=0)
        assert result == "custom/TST/packets"

    def test_falls_back_to_global(self):
        state = make_test_state(repeater_pub_key="AA" * 32)
        result = get_topic(state, "packets", broker_idx=0)
        assert result == f"meshcore/TST/{'AA' * 32}/packets"

    def test_broker_override_with_static_prefix(self):
        pubkey = "BB" * 32
        config = make_config()
        config['broker'][0]['topics'] = {
            'packets': f'meshrank/uplink/xxxxxx/{{PUBLIC_KEY}}/packets',
        }
        state = make_test_state(config=config, repeater_pub_key=pubkey)
        result = get_topic(state, "packets", broker_idx=0)
        assert result == f"meshrank/uplink/xxxxxx/{pubkey}/packets"


class TestSanitizeClientId:
    def test_alphanumeric(self):
        result = sanitize_client_id("TestNode123")
        assert result == "meshcore_TestNode123"

    def test_special_chars(self):
        result = sanitize_client_id("Test@Node#1")
        assert result == "meshcore_TestNode1"

    def test_spaces_to_underscores(self):
        result = sanitize_client_id("Test Node")
        assert result == "meshcore_Test_Node"

    def test_max_length(self):
        result = sanitize_client_id("A" * 100)
        assert len(result) == 23

    def test_custom_prefix(self):
        result = sanitize_client_id("Node", prefix="custom_")
        assert result == "custom_Node"

    def test_long_prefix_cannot_remove_node_identity(self):
        prefix = "meshcore_my_remote_site_"
        first = sanitize_client_id("AA" * 32, prefix)
        second = sanitize_client_id("BB" * 32, prefix)
        assert first != second
        assert len(first) <= 23
        assert len(second) <= 23
        assert first == sanitize_client_id("AA" * 32, prefix)

    def test_full_identity_not_only_public_key_prefix_is_hashed(self):
        first = sanitize_client_id("AA" * 32)
        second = sanitize_client_id("AA" * 31 + "BB")
        assert first != second

    def test_complete_prefix_namespace_is_in_identity_digest(self):
        first = sanitize_client_id('AA' * 32, 'remote_site_namespace_one_')
        second = sanitize_client_id('AA' * 32, 'remote_site_namespace_two_')
        assert first[:7] == second[:7] == 'remote_'
        assert first != second

    @pytest.mark.parametrize('prefix', ['', 'long-' * 20, '@#$%_' * 20])
    def test_long_ids_and_broker_suffix_stay_portable(self, prefix):
        ids = [sanitize_client_id('AA' * 32, prefix, suffix=f'_{index}')
               for index in (0, 1, 999999)]
        assert len(set(ids)) == len(ids)
        for client_id in ids:
            assert 1 <= len(client_id) <= 23
            assert all(char.isascii() and (char.isalnum() or char in '_-')
                       for char in client_id)


class TestGetBrokerConfig:
    def test_valid_index(self):
        state = make_test_state()
        broker = get_broker_config(state, 0)
        assert broker['name'] == 'test-broker'

    @pytest.mark.parametrize('index', [-99, -1, 99])
    def test_invalid_index(self, index):
        state = make_test_state()
        broker = get_broker_config(state, index)
        assert broker == {}
