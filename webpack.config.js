const MiniCssExtractPlugin = require('mini-css-extract-plugin');
const CssMinimizerPlugin = require("css-minimizer-webpack-plugin");
const { SourceMapDevToolPlugin } = require("webpack");
const path = require('path');

module.exports = {
    entry: './static/assets/index.js',  // path to our input file
    output: {
        filename: '[name].bundle.js',  // output bundle file name
        path: path.resolve(__dirname, './static/dist'),  // path to our Django static directory
        clean: true, // Clean the output directory before emit.
    },

    module: {
        rules: [
            {
                test: /\.css$/,
                use: [MiniCssExtractPlugin.loader, 'css-loader', 'postcss-loader'],
            },
            {
                test: /\.(png|jpg|jpeg|gif|svg)$/i,
                type: 'asset/resource',
                generator: {
                    filename: 'images/[hash][ext][query]'
                }
            },
            {
                test: /\.(woff|woff2|eot|ttf|otf)$/i,
                type: 'asset/resource',
                generator: {
                    filename: 'fonts/[hash][ext][query]'
                }
            },
        ],
    },
    resolve: {
        extensions: ['.js', '.jsx', '.css']
    },
    plugins: [
        new MiniCssExtractPlugin({
            filename: "[name].css",
        }),
        new SourceMapDevToolPlugin({
            filename: "[file].map"
        })
    ],
    optimization: {
        minimize: true,
        minimizer: [
            '...', // Extend existing minimizers (like Terser)
            new CssMinimizerPlugin(),
        ],
        splitChunks: {
            chunks: 'all',
            cacheGroups: {
                vendor: {
                    test: /[\\/]node_modules[\\/]/,
                    name: 'vendors',
                    chunks: 'all',
                },
            },
        },
    },
};