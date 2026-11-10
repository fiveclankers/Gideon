import asyncio
import json
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from gideon import AntigravityEngine, AudioStreamRecorder, GIDEON_VOICE_PREAMBLE


class TestInterruption(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.bus = MagicMock()
        self.tts = MagicMock(_is_speaking=False)
        self.tts.interrupt = AsyncMock()
        self.tts.wait_until_done = AsyncMock()
        self.engine = AntigravityEngine(self.bus, self.tts)
        self.engine.proc = MagicMock()
        self.engine.proc.returncode = None
        self.engine.proc.wait = AsyncMock(return_value=0)
        self.engine.proc.stdin.drain = AsyncMock()
        self.engine.proc.stdout = asyncio.StreamReader()

    async def asyncTearDown(self):
        await self.engine.close()

    async def test_interrupt_notifies_native_stdin_without_killing_process(self):
        await self.engine.send_prompt('Work on this')
        await self.engine.interrupt()
        payloads = [json.loads(c.args[0]) for c in self.engine.proc.stdin.write.call_args_list]
        self.assertEqual(len(payloads), 2)
        self.assertIn('Stop', payloads[-1]['message']['content'])
        self.engine.proc.terminate.assert_not_called()
        self.assertFalse(self.engine.is_turn_active)

    async def test_stalled_stdin_drain_does_not_block_next_input(self):
        blocked = asyncio.Event()
        self.engine.proc.stdin.drain.side_effect = blocked.wait
        await asyncio.wait_for(self.engine.send_prompt('Work'), timeout=.2)
        await asyncio.wait_for(self.engine.interrupt(), timeout=.2)
        self.assertFalse(self.engine.is_turn_active)

    async def test_old_result_cannot_finish_new_turn_or_speak_old_deltas(self):
        await self.engine.send_prompt('Old')
        await self.engine.interrupt()
        await self.engine.send_prompt('New')
        reader = asyncio.create_task(self.engine._read_loop())
        for event in [
            {'event': 'step_update', 'step_update': {'step_type': 'agent_response', 'text_delta': 'Stale speech.'}},
            {'event': 'result'}, {'event': 'result'},
        ]:
            self.engine.proc.stdout.feed_data((json.dumps(event)+'\n').encode())
        await asyncio.sleep(0)
        self.assertTrue(self.engine.is_turn_active)
        self.tts.queue_sentence.assert_not_called()
        self.engine.proc.stdout.feed_data(b'{"event":"result"}\n')
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        self.assertFalse(self.engine.is_turn_active)
        reader.cancel()
        await asyncio.gather(reader, return_exceptions=True)

    async def test_eof_clears_active_turn(self):
        await self.engine.send_prompt('Work')
        self.engine.proc.stdout.feed_eof()
        with patch.object(self.engine, 'start', new_callable=AsyncMock):
            await self.engine._read_loop()
        self.assertFalse(self.engine.is_turn_active)
        self.assertIsNone(self.engine.current_tool)

    async def test_old_tts_completion_does_not_rearm_hot_mic(self):
        finished = asyncio.Event()
        self.tts.wait_until_done.side_effect = finished.wait
        self.engine.wake_listener = MagicMock()
        await self.engine.send_prompt('Old')
        old_generation = self.engine._turn_generation
        completion = asyncio.create_task(self.engine._finish_turn(old_generation))
        await asyncio.sleep(0)
        await self.engine.send_prompt('New')
        finished.set()
        await completion
        self.assertTrue(self.engine.is_turn_active)
        self.engine.wake_listener.arm_hot_mic.assert_not_called()

    async def test_normal_result_finishes_and_rearms_45_second_hot_mic(self):
        self.engine.wake_listener = MagicMock()
        await self.engine.send_prompt('Hello')
        reader = asyncio.create_task(self.engine._read_loop())
        self.engine.proc.stdout.feed_data(b'{"event":"result"}\n')
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        self.assertFalse(self.engine.is_turn_active)
        self.engine.wake_listener.arm_hot_mic.assert_called_once_with(timeout_s=45.0)
        reader.cancel()
        await asyncio.gather(reader, return_exceptions=True)

    async def test_broken_pipe_returns_idle_without_losing_input_loop(self):
        self.engine.proc.stdin.write.side_effect = BrokenPipeError
        await self.engine.send_prompt('Hello')
        self.assertFalse(self.engine.is_turn_active)
        self.bus.set_state.assert_called_with('idle', 'My engine connection failed. Please try that again.')
        self.tts.queue_sentence.assert_called_with('My engine connection failed. Please try that again.')

    async def test_reader_failure_clears_active_turn(self):
        await self.engine.send_prompt('Hello')
        self.engine.proc.stdout = MagicMock()
        self.engine.proc.stdout.readline = AsyncMock(side_effect=ConnectionResetError)
        with patch.object(self.engine, 'start', new_callable=AsyncMock):
            await self.engine._read_loop()
        self.assertFalse(self.engine.is_turn_active)

    def replacement_process(self):
        proc = MagicMock(returncode=None)
        proc.wait = AsyncMock(return_value=0)
        proc.stdin.drain = AsyncMock()
        proc.stdout = asyncio.StreamReader()
        proc.stdout.feed_data(b'{"conversation_id":"saved-chat"}\n')
        return proc

    async def test_eof_warns_respawns_and_next_prompt_works(self):
        self.engine.conversation_id = 'saved-chat'
        await self.engine.send_prompt('Work')
        old = self.engine.proc
        old.returncode = 17
        old.stdout.feed_eof()
        replacement = self.replacement_process()
        with patch('gideon.asyncio.create_subprocess_exec', AsyncMock(return_value=replacement)) as spawn, \
                self.assertLogs('gideon', level='WARNING') as logs:
            await self.engine._read_loop()
        self.assertIn('17', '\n'.join(logs.output))
        self.assertIn('--conversation', spawn.call_args.args)
        self.assertIn('saved-chat', spawn.call_args.args)
        self.assertFalse(self.engine.is_turn_active)
        self.assertIsNone(self.engine._watchdog_task)
        self.assertEqual(list(self.engine._pending_turns), [])
        self.assertIsNotNone(self.engine._read_task)
        self.tts.queue_sentence.assert_called()
        await self.engine.send_prompt('Next')
        self.assertTrue(self.engine.is_turn_active)
        replacement.stdin.write.assert_called_once()
        self.assertEqual(list(self.engine._pending_turns), [self.engine._turn_generation])

    async def test_dead_proc_restarts_before_send_and_cancels_stale_drain(self):
        old = self.engine.proc
        old.returncode = 9
        stale_drain = asyncio.create_task(asyncio.Event().wait())
        self.engine._drain_task = stale_drain
        replacement = self.replacement_process()
        with patch('gideon.asyncio.create_subprocess_exec', AsyncMock(return_value=replacement)):
            await self.engine.send_prompt('Can you hear me?')
        old.stdin.write.assert_not_called()
        replacement.stdin.write.assert_called_once()
        self.assertTrue(stale_drain.cancelled())
        self.assertIsNot(self.engine._drain_task, stale_drain)
        self.assertTrue(self.engine.is_turn_active)

    async def test_interrupt_on_dead_engine_does_not_queue_orphan_stop(self):
        await self.engine.send_prompt('Old task')
        old = self.engine.proc
        old.returncode = 1
        replacement = self.replacement_process()
        with patch('gideon.asyncio.create_subprocess_exec', AsyncMock(return_value=replacement)):
            await self.engine.interrupt()
        replacement.stdin.write.assert_not_called()
        self.assertEqual(list(self.engine._pending_turns), [])
        await self.engine.send_prompt('New task')
        self.assertEqual(list(self.engine._pending_turns), [self.engine._turn_generation])

    async def test_write_guard_recovers_when_child_dies_after_send_alive_check(self):
        self.engine._is_first_turn = False
        self.engine._turn_generation = 4
        self.engine.is_turn_active = True
        old = self.engine.proc
        old.returncode = 1
        replacement = self.replacement_process()
        with patch('gideon.asyncio.create_subprocess_exec', AsyncMock(return_value=replacement)):
            self.assertTrue(await self.engine._write_prompt('New question', 4))
        old.stdin.write.assert_not_called()
        self.assertIn(GIDEON_VOICE_PREAMBLE, json.loads(replacement.stdin.write.call_args.args[0])['message']['content'])
        self.assertEqual(list(self.engine._pending_turns), [self.engine._turn_generation])
        self.assertTrue(self.engine.is_turn_active)

    async def test_resume_failure_starts_fresh_and_announces_context_loss_once(self):
        self.engine.conversation_id = 'saved-chat'
        self.engine._is_first_turn = False
        self.engine.proc.returncode = 1
        rejected = self.replacement_process()
        rejected.stdout = asyncio.StreamReader()
        rejected.stdout.feed_eof()
        rejected.returncode = 2
        replacement = self.replacement_process()
        with patch('gideon.asyncio.create_subprocess_exec', AsyncMock(side_effect=[rejected, replacement])) as spawn:
            await self.engine.send_prompt('Hello again')
        self.assertIn('--conversation', spawn.call_args_list[0].args)
        self.assertNotIn('--conversation', spawn.call_args_list[1].args)
        notices = [call.args[0] for call in self.tts.queue_sentence.call_args_list]
        self.assertEqual(sum('fresh conversation' in notice for notice in notices), 1)
        self.assertIn(GIDEON_VOICE_PREAMBLE, json.loads(replacement.stdin.write.call_args.args[0])['message']['content'])

    async def test_reconnect_failure_is_visible_and_next_prompt_retries(self):
        self.engine.proc.returncode = 1
        with patch('gideon.asyncio.create_subprocess_exec', AsyncMock(side_effect=FileNotFoundError)):
            await self.engine.send_prompt('Hello')
        self.assertFalse(self.engine.is_turn_active)
        self.assertIn("couldn't reconnect", self.bus.current_reply)
        replacement = self.replacement_process()
        with patch('gideon.asyncio.create_subprocess_exec', AsyncMock(return_value=replacement)):
            await self.engine.send_prompt('Retry')
        replacement.stdin.write.assert_called_once()

    async def test_late_eof_from_old_reader_cannot_reset_replacement_turn(self):
        old = self.engine.proc
        reader = asyncio.create_task(self.engine._read_loop())
        await asyncio.sleep(0)
        self.engine.proc = self.replacement_process()
        await self.engine.send_prompt('New child')
        generation = self.engine._turn_generation
        with patch('gideon.asyncio.create_subprocess_exec', new_callable=AsyncMock) as spawn:
            old.stdout.feed_eof()
            await reader
        spawn.assert_not_called()
        self.assertTrue(self.engine.is_turn_active)
        self.assertEqual(list(self.engine._pending_turns), [generation])

    async def test_close_during_reconnect_cancels_replacement_reader(self):
        self.engine.proc.returncode = 1
        self.engine.proc.stdout.feed_eof()
        spawning = asyncio.Event()
        release = asyncio.Event()
        replacement = self.replacement_process()

        async def spawn(*args, **kwargs):
            spawning.set()
            await release.wait()
            return replacement

        with patch('gideon.asyncio.create_subprocess_exec', side_effect=spawn):
            self.engine._read_task = asyncio.create_task(self.engine._read_loop())
            recovery = self.engine._read_task
            await asyncio.wait_for(spawning.wait(), timeout=1)
            closing = asyncio.create_task(self.engine.close())
            await asyncio.sleep(0)
            release.set()
            await asyncio.wait_for(asyncio.gather(recovery, closing), timeout=1)
        self.assertTrue(self.engine._closed)
        self.assertIsNone(self.engine._read_task)
        self.assertFalse(self.engine.is_turn_active)

    async def test_recorder_stop_failure_still_closes_callback_stream(self):
        recorder = AudioStreamRecorder(self.bus)
        with patch('sounddevice.InputStream'):
            recorder.start()
            owner = recorder.stream
            owner.stop.side_effect = RuntimeError('stop failed')
            recorder.stop()
            owner.close.assert_called_once()
            self.assertIsNone(recorder.stream)

    async def test_audio_open_failure_never_globally_resets_other_streams(self):
        from audio.wake import AudioListener
        listener = AudioListener()
        listener.wakeword_model = MagicMock()
        with patch('sounddevice.InputStream', side_effect=RuntimeError('device unavailable')), \
                patch('sounddevice._terminate') as terminate, \
                patch('sounddevice._initialize') as initialize:
            with self.assertRaises(RuntimeError):
                await listener.listen_for_command(is_hot_mic=False, is_speaking=False)
            terminate.assert_not_called()
            initialize.assert_not_called()

    async def test_duplicate_recorder_start_keeps_callback_owner_alive(self):
        recorder = AudioStreamRecorder(self.bus)
        with patch('sounddevice.InputStream') as stream:
            recorder.start()
            owner = recorder.stream
            recorder.start()
            self.assertIs(recorder.stream, owner)
            stream.assert_called_once()
            recorder.stop()
            owner.close.assert_called_once()

    def test_observed_browser_guardrails(self):
        for phrase in ('observed tab', 'exact URL', 'verify', 'NEVER invent'):
            self.assertIn(phrase, GIDEON_VOICE_PREAMBLE)
