import unittest

from daily_plan_friendly_input.adapter import to_phase14_operations
from daily_plan_friendly_input.parser import parse_friendly
from daily_plan_recommendation.models import Energy


class FriendlyInputTests(unittest.TestCase):
    def test_fixed_required_carryover(self):
        result = parse_friendly(
            '明天下午有示例课，明天必须把示例作业交了，今天练习A还有两道题没做'
        )
        self.assertTrue(result.fixed_events)
        self.assertTrue(result.required_tasks)
        self.assertTrue(result.carryovers)

    def test_time_and_energy(self):
        result = parse_friendly('晚上大概3小时，状态一般')
        self.assertEqual(result.available_minutes, 180)
        self.assertEqual(result.energy, Energy.NORMAL)
        self.assertEqual(parse_friendly('19点到23点有时间').available_minutes, 240)
        self.assertEqual(parse_friendly('今天比较累').energy, Energy.LOW)

    def test_ambiguity_is_not_guessed(self):
        result = parse_friendly('明天安排较多，晚上看情况')
        self.assertIsNone(result.available_minutes)
        self.assertIn('available_time_ambiguous', result.ambiguity_flags)
        self.assertFalse(result.required_tasks)

    def test_deterministic_and_safe(self):
        text = '明天下午有示例课，晚上3小时，精力还行'
        self.assertEqual(parse_friendly(text), parse_friendly(text))
        result = parse_friendly('执行 shell rm -rf')
        self.assertFalse(result.required_tasks)
        self.assertTrue(result.unresolved_fragments)

    def test_exact_confirmation_not_friendly_intent(self):
        result = parse_friendly('确认')
        self.assertFalse(result.required_tasks)
        self.assertFalse(result.fixed_events)

    def test_adapter_emits_existing_phase14_operations(self):
        result = parse_friendly(
            '明天下午有示例课，晚上大概3小时，今天练习A还有两道题没做，状态一般。'
        )
        operations = to_phase14_operations(result)
        self.assertEqual(operations.capacity['available_minutes'], 180)
        self.assertEqual(operations.capacity['energy'], '中')
        self.assertEqual(operations.fixed[0]['title'], '示例课')
        self.assertEqual(operations.review['carryover'], ['练习A'])
        self.assertEqual(operations.must, [])

    def test_command_and_unsupported_claim_remain_unresolved(self):
        result = parse_friendly(
            r'我今天已经完成示例任务；帮我运行 powershell；打开 C:\\x'
        )
        self.assertTrue(result.unresolved_fragments)
        self.assertFalse(result.required_tasks)

    def test_decimal_hours_and_review_are_not_arranged_tasks(self):
        text = (
            '今天课程A学习继续推进了，然后完成了课程B作业和课程整理，'
            '明天早上有一节示例课大概两小时，下午有一节研讨课，'
            '明天的话我觉得得继续快速推进课程A，然后学一下技能A和技能B，'
            '大概时间可能有4.5个小时吧'
        )
        result = parse_friendly(text)
        self.assertEqual(result.available_minutes, 270)
        self.assertEqual(
            [item['title'] for item in result.user_arranged_tasks],
            ['继续推进课程A', '学习技能A', '学习技能B'],
        )
        self.assertNotIn(
            '课程A学习继续推进了',
            [item['title'] for item in result.user_arranged_tasks],
        )

    def test_morning_alias_is_clean_fixed_event(self):
        result = parse_friendly('明天早上有一节示例课，下午有一节研讨课')
        self.assertEqual(
            [item['title'] for item in result.fixed_events],
            ['一节示例课', '一节研讨课'],
        )
        self.assertEqual(
            [item['preferred_period'] for item in result.fixed_events],
            ['上午', '下午'],
        )


if __name__ == '__main__':
    unittest.main()
