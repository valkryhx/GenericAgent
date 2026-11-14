import { tabBarText } from '../src/questionPanel.js'

const p = {
  requestId: 'r',
  questions: [
    { key: 'a', question: '?', header: 'Database choice' },
    { key: 'b', question: '?', header: 'Region' },
    { key: 'c', question: '?', header: 'Strategy' },
  ],
  currentIndex: 1,
  answers: {},
  textInput: {},
  editingKey: null,
  optionIndex: 0,
  hideSubmitTab: false,
}

for (const c of [120, 80, 60, 45, 40, 30, 20, 12]) {
  console.log(String(c).padStart(3), '|' + tabBarText(p as never, c) + '|')
}
