/** @type {import('tailwindcss').Config} */
export default {
  content: ['./index.html', './src/**/*.{js,ts,jsx,tsx}'],
  theme: {
    extend: {
      colors: {
        wechat: {
          green: '#07c160',
          'green-dark': '#06ad56',
          bg: '#f5f7f6',
          'bubble-self': '#95ec69',
          'bubble-other': '#ffffff',
        },
      },
    },
  },
  plugins: [],
};
